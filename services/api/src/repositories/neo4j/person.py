"""Neo4j implementation of PersonRepository."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from time import monotonic
from typing import Literal, cast
from uuid import uuid4

from neo4j import AsyncManagedTransaction

from src.config import config
from src.display_format import format_display_datetime
from src.graph.client import get_session
from src.graph.converters import GraphRecord, to_int, to_iso_or_none, to_str
from src.graph.mappers import (
    _map_loyalty,
    _map_vehicles,
    map_audit_event,
    map_bankruptcy_case,
    map_connection,
    map_match_decision,
    map_person,
    map_person_graph,
    map_person_identifier,
    map_possible_match_detail,
    map_shared_identifier_candidate,
    map_source_record,
    map_source_record_entity_facet,
    map_timeline_group,
)
from src.graph.mappers_entities import map_listed_person, map_person_entity
from src.graph.mappers_profile_analysis import (
    map_person_profile_analyses,
    map_profile_analysis_history_item,
)
from src.graph.queries import (
    COUNT_PERSON_AUDIT,
    COUNT_PERSON_BANKRUPTCY_CASES,
    COUNT_PERSON_CONNECTIONS_ADDRESS,
    COUNT_PERSON_CONNECTIONS_ALL,
    COUNT_PERSON_CONNECTIONS_IDENTIFIER,
    COUNT_PERSON_CONNECTIONS_KNOWS,
    COUNT_PERSON_IDENTIFIERS,
    COUNT_PERSON_MATCHES,
    COUNT_PERSON_SHARED_IDENTIFIERS,
    COUNT_PERSON_SOURCE_RECORDS,
    COUNT_PERSON_TIMELINE,
    CREATE_FAILED_PROFILE_ANALYSIS_RETRY,
    CREATE_PROFILE_ANALYSIS_REQUEST,
    FIND_PERSON_BY_IDENTIFIER,
    GET_PERSON_AUDIT,
    GET_PERSON_BANKRUPTCY_CASES,
    GET_PERSON_BY_ID,
    GET_PERSON_CONNECTIONS_ADDRESS,
    GET_PERSON_CONNECTIONS_ALL,
    GET_PERSON_CONNECTIONS_IDENTIFIER,
    GET_PERSON_CONNECTIONS_KNOWS,
    GET_PERSON_ENTITIES,
    GET_PERSON_IDENTIFIERS,
    GET_PERSON_LIST_CORE_SUMMARY,
    GET_PERSON_LIST_CRM_SUMMARY,
    GET_PERSON_LIST_SUMMARY_COUNTER,
    GET_PERSON_LOYALTY,
    GET_PERSON_MATCHES,
    GET_PERSON_POSSIBLE_MATCH_DETAIL,
    GET_PERSON_PROFILE_ANALYSES,
    GET_PERSON_PROFILE_ANALYSIS_HISTORY,
    GET_PERSON_SHARED_IDENTIFIERS,
    GET_PERSON_SOURCE_RECORD_ENTITY_FACETS,
    GET_PERSON_SOURCE_RECORDS,
    GET_PERSON_TIMELINE,
    GET_PERSON_TIMELINE_TARGET,
    GET_PERSON_VEHICLES,
    REQUEUE_FAILED_PROFILE_ANALYSIS_REQUEST,
    SEARCH_PERSONS,
    build_count_persons_query,
    build_list_persons_query,
    get_graph_query,
    get_node_graph_query,
)
from src.graph.queries.person_list_summary import (
    GET_PERSON_LIST_SUMMARY_CURRENT_MONTH,
    PERSON_LIST_SUMMARY_COUNTER_KEY,
)
from src.nric import canonical_nric, nric_digest
from src.repositories.protocols.person import PersonListFilters, PersonPage
from src.request_timing import create_detached_task
from src.types import (
    AuditEvent,
    BankruptcyCase,
    ConnectionType,
    LoyaltySummary,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    PersonListCoreSummary,
    PersonListCrmSummary,
    PersonListSummary,
    PersonSharedIdentifierCandidate,
    PersonTimelineGroup,
    PossibleMatchDetail,
    SourceRecord,
    SourceRecordEntityFacet,
    VehicleSummary,
)
from src.types_profile_analysis import (
    PROFILE_ANALYSIS_USER_RETRY_LIMIT,
    PersonProfileAnalyses,
    ProfileAnalysisHistoryItem,
    ProfileAnalysisRequestRequeueResult,
    ProfileAnalysisRequestResult,
    ProfileAnalysisRetryResult,
    ProfileAnalysisType,
)

from ._utils import record_to_dict, to_total


def _connection_query(connection_type: ConnectionType) -> str:
    if connection_type is ConnectionType.IDENTIFIER:
        return GET_PERSON_CONNECTIONS_IDENTIFIER
    if connection_type is ConnectionType.ADDRESS:
        return GET_PERSON_CONNECTIONS_ADDRESS
    if connection_type is ConnectionType.KNOWS:
        return GET_PERSON_CONNECTIONS_KNOWS
    return GET_PERSON_CONNECTIONS_ALL


def _connection_count_query(connection_type: ConnectionType) -> str:
    if connection_type is ConnectionType.IDENTIFIER:
        return COUNT_PERSON_CONNECTIONS_IDENTIFIER
    if connection_type is ConnectionType.ADDRESS:
        return COUNT_PERSON_CONNECTIONS_ADDRESS
    if connection_type is ConnectionType.KNOWS:
        return COUNT_PERSON_CONNECTIONS_KNOWS
    return COUNT_PERSON_CONNECTIONS_ALL


async def _request_profile_analysis_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    analysis_type: ProfileAnalysisType,
    force: bool,
    request_id: str,
) -> GraphRecord | None:
    result = await tx.run(
        CREATE_PROFILE_ANALYSIS_REQUEST,
        person_id=person_id,
        analysis_type=analysis_type,
        force=force,
        request_id=request_id,
    )
    record = await result.single()
    if record is None:
        return None
    return record_to_dict(record.keys(), list(record.values()))


async def _retry_failed_profile_analysis_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    analysis_type: ProfileAnalysisType,
    retry_actor_id: str,
    max_retries: int,
    request_id: str,
) -> GraphRecord | None:
    result = await tx.run(
        CREATE_FAILED_PROFILE_ANALYSIS_RETRY,
        person_id=person_id,
        analysis_type=analysis_type,
        retry_actor_id=retry_actor_id,
        max_retries=max_retries,
        request_id=request_id,
    )
    record = await result.single()
    if record is None:
        return None
    return record_to_dict(record.keys(), list(record.values()))


def _core_summary_fields(summary: PersonListCoreSummary) -> dict[str, int]:
    """Normalize legacy test/double values to the independently cached core contract."""
    return {
        "all_profiles_count": summary.all_profiles_count,
        "high_risk_count": summary.high_risk_count,
        "high_value_count": summary.high_value_count,
        "no_contact_count": summary.no_contact_count,
    }


def _optional_count(value: object) -> int | None:
    """Return a stored counter value when it is a usable integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _optional_text(value: object) -> str | None:
    """Return a stored counter value when it is a string."""
    if isinstance(value, str):
        return value
    return None


@dataclass(frozen=True)
class _CounterState:
    """The maintained counter's keyed state, read once per public summary call.

    Every validity flag is strict: a missing singleton, an unready counter, a
    malformed value or a stale month marker leaves the corresponding fast path
    unusable, so the retained aggregates answer instead.
    """

    ready: bool
    generation: int | None
    generation_valid: bool
    month: str | None
    month_valid: bool
    current_month: str
    core_counts_valid: bool
    crm_counts_valid: bool
    active_person_count: int | None
    high_risk_count: int | None
    high_value_count: int | None
    no_contact_count: int | None
    deals_this_month_count: int | None
    all_deals_count: int | None

    @property
    def core_usable(self) -> bool:
        """Whether the counter may serve the four core counts."""
        return self.ready and self.generation_valid and self.core_counts_valid

    @property
    def crm_usable(self) -> bool:
        """Whether the counter may serve the two CRM counts."""
        return self.ready and self.generation_valid and self.crm_counts_valid and self.month_valid

    @classmethod
    def from_values(cls, values: GraphRecord) -> _CounterState:
        """Build the state from one keyed counter row."""
        return cls(
            ready=values.get("ready") is True,
            generation=_optional_count(values.get("generation")),
            generation_valid=values.get("generation_valid") is True,
            month=_optional_text(values.get("month")),
            month_valid=values.get("month_valid") is True,
            current_month=_optional_text(values.get("current_month")) or "",
            core_counts_valid=values.get("core_counts_valid") is True,
            crm_counts_valid=values.get("crm_counts_valid") is True,
            active_person_count=_optional_count(values.get("active_person_count")),
            high_risk_count=_optional_count(values.get("high_risk_count")),
            high_value_count=_optional_count(values.get("high_value_count")),
            no_contact_count=_optional_count(values.get("no_contact_count")),
            deals_this_month_count=_optional_count(values.get("deals_this_month_count")),
            all_deals_count=_optional_count(values.get("all_deals_count")),
        )


@dataclass(frozen=True)
class _CrmFallback:
    """One legacy CRM aggregate value and the counter state it was computed under."""

    values: tuple[int, int]
    generation: int | None
    month: str


class Neo4jPersonRepository:
    def __init__(self) -> None:
        self._summary_cache: PersonListCoreSummary | None = None
        self._summary_cache_expires_at = 0.0
        self._summary_cache_generation: int | None = None
        self._summary_cache_lock = asyncio.Lock()
        self._crm_summary_cache: tuple[int, int] | None = None
        self._crm_summary_cache_expires_at = 0.0
        self._crm_summary_cache_generation: int | None = None
        self._crm_summary_cache_month: str | None = None
        self._crm_summary_cache_lock = asyncio.Lock()
        self._crm_summary_task: asyncio.Task[_CrmFallback | None] | None = None
        self._crm_refresh_sequence = 0
        self._counter_read_sequence = 0
        self._counter_state: _CounterState | None = None

    def _cached_summary(self) -> PersonListCoreSummary | None:
        if self._summary_cache is None or monotonic() >= self._summary_cache_expires_at:
            return None
        return PersonListCoreSummary.model_validate(self._summary_cache.model_dump())

    def _clear_core_cache(self) -> None:
        """Drop the cached legacy core aggregate."""
        self._summary_cache = None
        self._summary_cache_expires_at = 0.0
        self._summary_cache_generation = None

    def _clear_crm_cache(self) -> None:
        """Drop the cached legacy CRM aggregate."""
        self._crm_summary_cache = None
        self._crm_summary_cache_expires_at = 0.0
        self._crm_summary_cache_generation = None
        self._crm_summary_cache_month = None

    async def get_page(
        self,
        filters: PersonListFilters,
        skip: int,
        limit: int,
        *,
        include_total: bool,
    ) -> PersonPage:
        sort_by = filters.get("sort_by")
        sort_order = filters.get("sort_order")
        has_q = filters.get("q") is not None
        active_filters = frozenset(key for key, value in filters.items() if value is not None)
        entity_mode = filters.get("entity_key_mode") or "or"
        source_mode = filters.get("source_key_mode") or "or"
        list_query = build_list_persons_query(
            sort_by,
            sort_order,
            has_q=has_q,
            active_filters=active_filters,
            entity_mode=entity_mode,
            source_mode=source_mode,
        )
        # sort_by/sort_order/entity_key_mode/source_key_mode are used to build
        # the query string, not as Cypher params.
        cypher_params: dict[str, str | int | bool | list[str] | None] = {
            k: v  # type: ignore[misc]  # TypedDict values are known-safe filter keys
            for k, v in filters.items()
            if k not in ("sort_by", "sort_order", "entity_key_mode", "source_key_mode")
        }

        list_limit = limit if include_total else limit + 1

        async def _run_list() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    list_query,
                    {**cypher_params, "skip": skip, "limit": list_limit},
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        if not include_total:
            records = await _run_list()
            return PersonPage(
                items=[map_listed_person(record) for record in records[:limit]],
                has_more=len(records) > limit,
                total_count=None,
            )

        count_query = build_count_persons_query(
            sort_by,
            sort_order,
            has_q=has_q,
            active_filters=active_filters,
            entity_mode=entity_mode,
            source_mode=source_mode,
        )

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(count_query, cypher_params)
                return to_total(await result.single())

        records, total = await asyncio.gather(_run_list(), _run_count())
        return PersonPage(
            items=[map_listed_person(record) for record in records[:limit]],
            has_more=skip + limit < total,
            total_count=total,
        )

    async def _load_core_summary(self) -> PersonListCoreSummary:
        async with get_session() as session:
            result = await session.run(GET_PERSON_LIST_CORE_SUMMARY)
            record = await result.single()
        if record is None:
            return PersonListCoreSummary()
        values = record_to_dict(record.keys(), list(record.values()))
        return PersonListCoreSummary(
            all_profiles_count=to_int(values.get("all_profiles_count")),
            high_risk_count=to_int(values.get("high_risk_count")),
            high_value_count=to_int(values.get("high_value_count")),
            no_contact_count=to_int(values.get("no_contact_count")),
        )

    async def _load_crm_summary(self) -> tuple[int, int]:
        async with get_session() as session:
            result = await session.run(GET_PERSON_LIST_CRM_SUMMARY)
            record = await result.single()
        if record is None:
            return 0, 0
        values = record_to_dict(record.keys(), list(record.values()))
        return (
            to_int(values.get("deals_this_month_count")),
            to_int(values.get("all_deals_count")),
        )

    async def _read_counter_state(self) -> _CounterState:
        """Read the maintained counter's keyed state.

        The keyed read is the only counter read: the row exists even when the
        singleton does not, so the current month is always known and every
        validity flag is false in that case.

        Reads are sequenced: an older in-flight read that completes after a
        newer one must not overwrite the shared state the detached refresh
        compares against.
        """
        self._counter_read_sequence += 1
        sequence = self._counter_read_sequence
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_LIST_SUMMARY_COUNTER,
                key=PERSON_LIST_SUMMARY_COUNTER_KEY,
            )
            record = await result.single()
        values: GraphRecord = (
            record_to_dict(record.keys(), list(record.values())) if record is not None else {}
        )
        state = _CounterState.from_values(values)
        if sequence == self._counter_read_sequence:
            self._counter_state = state
        return state

    async def _read_database_month(self) -> str:
        """Read one live database-clock sample without rereading the counter node."""
        async with get_session() as session:
            result = await session.run(GET_PERSON_LIST_SUMMARY_CURRENT_MONTH)
            record = await result.single()
        current_month = _optional_text(record.get("current_month")) if record is not None else None
        if current_month is None:
            raise RuntimeError("person-list summary database clock returned no month")
        return current_month

    async def _recheck_counter_month(self, state: _CounterState) -> _CounterState:
        """Refresh the month-validity view without another keyed-node read."""
        current_month = await self._read_database_month()
        return replace(
            state,
            current_month=current_month,
            month_valid=state.month == current_month,
        )

    def _apply_counter_state(self, state: _CounterState) -> None:
        """Invalidate the fallback caches the counter state no longer matches.

        A generation change (a reset or an invalidation) drops both caches; a
        month change drops the CRM cache, whose month-scoped value is stale. A
        usable fast path drops the matching cache as well: the counter answers
        now.
        """
        if state.core_usable or (
            self._summary_cache is not None and self._summary_cache_generation != state.generation
        ):
            self._clear_core_cache()
        if state.crm_usable or (
            self._crm_summary_cache is not None
            and (
                self._crm_summary_cache_generation != state.generation
                or self._crm_summary_cache_month != state.current_month
            )
        ):
            self._clear_crm_cache()

    @staticmethod
    def _core_from_counter(state: _CounterState) -> PersonListCoreSummary:
        """Build the core summary from the maintained counter."""
        return PersonListCoreSummary(
            all_profiles_count=state.active_person_count or 0,
            high_risk_count=state.high_risk_count or 0,
            high_value_count=state.high_value_count or 0,
            no_contact_count=state.no_contact_count or 0,
        )

    @staticmethod
    def _crm_from_counter(state: _CounterState) -> tuple[int, int]:
        """Build the CRM summary from the maintained counter."""
        return (state.deals_this_month_count or 0, state.all_deals_count or 0)

    async def _load_core_summary_guarded(
        self,
        state: _CounterState,
        *,
        attempts: int = 2,
    ) -> tuple[PersonListCoreSummary, _CounterState]:
        """Load the legacy core aggregate and the counter state it is valid under.

        The aggregate may straddle a counter reset or a generation bump, so the
        keyed state is re-read afterwards: a moved generation is recomputed
        once against the fresh state, and a counter that became usable in the
        meantime supplies the result. When the generation moved again on the
        last allowed attempt the aggregate is rejected rather than returned.
        """
        if state.core_usable:
            return self._core_from_counter(state), state
        summary = await self._load_core_summary()
        after = await self._read_counter_state()
        if after.core_usable:
            return self._core_from_counter(after), after
        if after.generation == state.generation:
            return summary, after
        if attempts <= 1:
            raise RuntimeError("person-list summary core aggregate raced the counter generation")
        return await self._load_core_summary_guarded(after, attempts=attempts - 1)

    async def _load_crm_summary_guarded(
        self,
        state: _CounterState,
        *,
        attempts: int = 2,
    ) -> tuple[tuple[int, int], _CounterState]:
        """Load the legacy CRM aggregate and the counter state it is valid under.

        A month rollover invalidates the month-scoped value just like a
        generation change invalidates the whole counter, so both are rechecked
        after the aggregate ran. When either moved on the last allowed
        attempt the aggregate is rejected rather than returned.
        """
        if state.crm_usable:
            return self._crm_from_counter(state), state
        crm = await self._load_crm_summary()
        after = await self._read_counter_state()
        if after.crm_usable:
            return self._crm_from_counter(after), after
        if after.generation == state.generation and after.current_month == state.current_month:
            return crm, after
        if attempts <= 1:
            raise RuntimeError(
                "person-list summary CRM aggregate raced the counter generation or month"
            )
        return await self._load_crm_summary_guarded(after, attempts=attempts - 1)

    async def _core_summary_for(self, state: _CounterState) -> PersonListCoreSummary:
        """Return the core summary: the counter when usable, else the legacy aggregate."""
        if state.core_usable:
            return self._core_from_counter(state)
        ttl = config.person_list_summary_cache_ttl_seconds
        if ttl <= 0:
            summary, _final = await self._load_core_summary_guarded(state)
            return summary
        cached = self._cached_summary()
        if cached is not None:
            return cached
        async with self._summary_cache_lock:
            latest = await self._read_counter_state()
            self._apply_counter_state(latest)
            if latest.core_usable:
                return self._core_from_counter(latest)
            cached = self._cached_summary()
            if cached is not None:
                return cached
            summary, final = await self._load_core_summary_guarded(latest)
            self._summary_cache = summary
            self._summary_cache_expires_at = monotonic() + ttl
            self._summary_cache_generation = final.generation
            return summary

    async def _crm_summary_for(self, state: _CounterState) -> tuple[int, int]:
        """Return the CRM summary: the counter when usable, else the legacy aggregate."""
        if state.crm_usable:
            return self._crm_from_counter(state)
        ttl = config.person_list_summary_cache_ttl_seconds
        if ttl <= 0:
            crm, _final = await self._load_crm_summary_guarded(state)
            return crm
        cached = self._cached_crm_summary()
        if cached is not None:
            return cached
        if self._crm_summary_cache is not None:
            self._start_crm_summary_refresh(ttl, state)
            return self._crm_summary_cache
        async with self._crm_summary_cache_lock:
            latest = await self._read_counter_state()
            self._apply_counter_state(latest)
            if latest.crm_usable:
                return self._crm_from_counter(latest)
            cached = self._cached_crm_summary()
            if cached is not None:
                return cached
            crm, final = await self._load_crm_summary_guarded(latest)
            self._crm_summary_cache = crm
            self._crm_summary_cache_generation = final.generation
            self._crm_summary_cache_month = final.current_month
            self._crm_summary_cache_expires_at = monotonic() + ttl
            return crm

    async def _load_list_summary(self, state: _CounterState) -> PersonListSummary:
        """Load an exact, uncached combined summary for explicit zero-TTL use.

        The core and the CRM aggregate are compared before they are combined:
        a response must never mix two counter generations or two months, so a
        disagreeing pair is recomputed once from a fresh state.
        """
        for attempt in range(2):
            (core, core_state), (crm, crm_state) = await asyncio.gather(
                self._load_core_summary_guarded(state),
                self._load_crm_summary_guarded(state),
            )
            if core_state.generation == crm_state.generation and (
                core_state.current_month == crm_state.current_month
            ):
                return PersonListSummary(
                    **_core_summary_fields(core),
                    deals_this_month_count=crm[0],
                    all_deals_count=crm[1],
                )
            if attempt == 0:
                state = await self._read_counter_state()
                self._apply_counter_state(state)
                continue
            raise RuntimeError(
                "person-list summary core and CRM aggregates disagree on the counter state"
            )
        raise RuntimeError("person-list summary aggregates did not settle on one counter state")

    async def _list_summary_fallback(self, state: _CounterState) -> PersonListSummary:
        """Return the combined summary from the aggregates, never mixing generations.

        The keyed state is re-read after both loads: a generation or month that
        moved while they ran means the two halves may disagree, so the pair is
        rebuilt once from the fresh state.
        """
        ttl = config.person_list_summary_cache_ttl_seconds
        if ttl <= 0:
            return await self._load_list_summary(state)
        for attempt in range(2):
            core = await self._core_summary_for(state)
            crm = await self._crm_summary_for(state)
            after = await self._read_counter_state()
            self._apply_counter_state(after)
            if after.generation == state.generation and (
                after.current_month == state.current_month
            ):
                return PersonListSummary(
                    **_core_summary_fields(core),
                    deals_this_month_count=crm[0],
                    all_deals_count=crm[1],
                )
            if attempt == 0:
                state = after
                continue
            raise RuntimeError("person-list summary aggregates raced the counter state")
        raise RuntimeError("person-list summary aggregates did not settle on one counter state")

    def _cached_crm_summary(self) -> tuple[int, int] | None:
        if self._crm_summary_cache is None or monotonic() >= self._crm_summary_cache_expires_at:
            return None
        return self._crm_summary_cache

    async def _refresh_crm_summary(self, state: _CounterState) -> _CrmFallback | None:
        """Recompute the legacy CRM aggregate in the background, guarded by the state."""
        values = await self._load_crm_summary()
        after = await self._read_counter_state()
        if after.crm_usable:
            return None
        if after.generation != state.generation or after.current_month != state.current_month:
            return None
        confirmed = await self._read_counter_state()
        if confirmed.crm_usable:
            return None
        if (
            confirmed.generation != after.generation
            or confirmed.current_month != after.current_month
        ):
            return None
        return _CrmFallback(
            values=values,
            generation=confirmed.generation,
            month=confirmed.current_month,
        )

    def _start_crm_summary_refresh(self, ttl: int, state: _CounterState) -> None:
        if self._crm_summary_task is not None and not self._crm_summary_task.done():
            return
        self._crm_refresh_sequence += 1
        sequence = self._crm_refresh_sequence
        task = create_detached_task(self._refresh_crm_summary(state), background_read=True)
        self._crm_summary_task = task

        def store_result(completed: asyncio.Task[_CrmFallback | None]) -> None:
            if self._crm_summary_task is completed:
                self._crm_summary_task = None
            if completed.cancelled():
                return
            try:
                fallback = completed.result()
            except Exception:
                # The core summary remains available; retain a prior exact CRM
                # value rather than exposing a CRM scan failure to list users.
                return
            if fallback is None:
                return
            if sequence != self._crm_refresh_sequence:
                # A newer refresh superseded this one: a stale completion must
                # not overwrite the newer cache.
                return
            current = self._counter_state
            if current is not None and (
                current.crm_usable
                or fallback.generation != current.generation
                or fallback.month != current.current_month
            ):
                # The counter moved on (or became authoritative) while the
                # refresh ran: a stale completion must not overwrite the cache.
                return
            self._crm_summary_cache = fallback.values
            self._crm_summary_cache_generation = fallback.generation
            self._crm_summary_cache_month = fallback.month
            self._crm_summary_cache_expires_at = monotonic() + ttl

        task.add_done_callback(store_result)

    async def get_list_summary(self) -> PersonListSummary:
        """Return the combined list summary: the counter when usable, else the aggregates."""
        state = await self._read_counter_state()
        self._apply_counter_state(state)
        if state.core_usable and state.crm_usable:
            latest = await self._recheck_counter_month(state)
            self._apply_counter_state(latest)
            if latest.core_usable and latest.crm_usable:
                return PersonListSummary(
                    **_core_summary_fields(self._core_from_counter(latest)),
                    deals_this_month_count=latest.deals_this_month_count or 0,
                    all_deals_count=latest.all_deals_count or 0,
                )
            return await self._list_summary_fallback(latest)
        return await self._list_summary_fallback(state)

    async def get_list_core_summary(self) -> PersonListCoreSummary:
        """Return the core summary without waiting for the CRM aggregate."""
        state = await self._read_counter_state()
        self._apply_counter_state(state)
        return await self._core_summary_for(state)

    async def _crm_summary_with_boundary_recheck(self, state: _CounterState) -> tuple[int, int]:
        """Recheck the live database month before returning a counter or fallback."""
        if state.crm_usable:
            state = await self._recheck_counter_month(state)
            self._apply_counter_state(state)
            if state.crm_usable:
                return self._crm_from_counter(state)
        for _attempt in range(2):
            crm = await self._crm_summary_for(state)
            latest = await self._read_counter_state()
            self._apply_counter_state(latest)
            if latest.crm_usable:
                latest = await self._recheck_counter_month(latest)
                self._apply_counter_state(latest)
                if latest.crm_usable:
                    return self._crm_from_counter(latest)
            if (
                latest.generation == state.generation
                and latest.current_month == state.current_month
            ):
                if not state.crm_usable:
                    return crm
                crm, final = await self._load_crm_summary_guarded(latest)
                self._apply_counter_state(final)
                if final.crm_usable:
                    final = await self._recheck_counter_month(final)
                    self._apply_counter_state(final)
                    if final.crm_usable:
                        return self._crm_from_counter(final)
                if (
                    final.generation == latest.generation
                    and final.current_month == latest.current_month
                ):
                    return crm
                latest = final
            state = latest
        raise RuntimeError("person-list CRM summary raced the counter generation or month")

    async def get_list_crm_summary(self) -> PersonListCrmSummary:
        """Return the CRM summary, awaiting the first cache fill when needed."""
        state = await self._read_counter_state()
        self._apply_counter_state(state)
        crm = await self._crm_summary_with_boundary_recheck(state)
        return PersonListCrmSummary(
            deals_this_month_count=crm[0],
            all_deals_count=crm[1],
        )

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        if identifier_type == "nric":
            canonical = canonical_nric(value)
            digest = nric_digest(canonical)
            values = [digest, canonical] if digest is not None else [canonical]
        else:
            values = [value]
        async with get_session() as session:
            result = await session.run(
                FIND_PERSON_BY_IDENTIFIER, identifier_type=identifier_type, values=values
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_person(rec) for rec in records]

    async def search_by_query(
        self, q: str, status: str | None, skip: int, limit: int
    ) -> tuple[list[Person], bool]:
        async with get_session() as session:
            result = await session.run(
                SEARCH_PERSONS,
                {"query": q, "status": status, "skip": skip, "limit": limit + 1},
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > limit
        return [map_person(rec) for rec in records[:limit]], has_more

    async def get_by_id(self, person_id: str) -> Person | None:
        async with get_session() as session:
            result = await session.run(GET_PERSON_BY_ID, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        return map_person(record_to_dict(record.keys(), list(record.values())))

    async def get_profile_analyses(
        self, person_id: str, retry_actor_id: str
    ) -> PersonProfileAnalyses | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_PROFILE_ANALYSES,
                person_id=person_id,
                retry_actor_id=retry_actor_id,
                max_user_retries=PROFILE_ANALYSIS_USER_RETRY_LIMIT,
            )
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        return map_person_profile_analyses(mapped)

    async def request_profile_analysis(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType,
        force: bool,
    ) -> ProfileAnalysisRequestResult | None:
        request_id = str(uuid4())
        async with get_session(write=True) as session:
            mapped = await session.execute_write(
                _request_profile_analysis_tx,
                person_id,
                analysis_type,
                force,
                request_id,
            )
        if mapped is None:
            return None
        state = to_str(mapped.get("state"))
        if state not in {"queued", "already_queued", "already_valid", "force_limited"}:
            raise ValueError("invalid profile analysis request state")
        request_state = cast(
            Literal["queued", "already_queued", "already_valid", "force_limited"],
            state,
        )
        available_at = to_iso_or_none(mapped.get("force_available_at"))
        return ProfileAnalysisRequestResult(
            request_id=to_iso_or_none(mapped.get("request_id")),
            person_id=to_str(mapped.get("person_id")),
            analysis_type=analysis_type,
            state=request_state,
            force=force,
            force_attempts_remaining=to_int(mapped.get("force_attempts_remaining")),
            force_available_at=available_at,
            force_available_at_display=(
                format_display_datetime(available_at) if available_at is not None else None
            ),
        )

    async def retry_failed_profile_analysis(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType,
        retry_actor_id: str,
    ) -> ProfileAnalysisRetryResult | None:
        request_id = str(uuid4())
        async with get_session(write=True) as session:
            mapped = await session.execute_write(
                _retry_failed_profile_analysis_tx,
                person_id,
                analysis_type,
                retry_actor_id,
                PROFILE_ANALYSIS_USER_RETRY_LIMIT,
                request_id,
            )
        if mapped is None:
            return None
        state = to_str(mapped.get("state"))
        valid_states = {"queued", "completed", "already_active", "not_failed", "retry_limited"}
        if state not in valid_states:
            raise ValueError("invalid profile analysis retry state")
        available_at = to_iso_or_none(mapped.get("retry_available_at"))
        return ProfileAnalysisRetryResult(
            request_id=to_iso_or_none(mapped.get("request_id")),
            person_id=to_str(mapped.get("person_id")),
            analysis_type=analysis_type,
            state=cast(
                Literal["queued", "completed", "already_active", "not_failed", "retry_limited"],
                state,
            ),
            retry_attempts_remaining=to_int(mapped.get("retry_attempts_remaining")),
            retry_available_at=available_at,
            retry_available_at_display=(
                format_display_datetime(available_at) if available_at is not None else None
            ),
        )

    async def requeue_failed_profile_analysis_request(
        self,
        person_id: str,
        request_id: str,
        max_attempts: int,
    ) -> ProfileAnalysisRequestRequeueResult | None:
        async with get_session(write=True) as session:
            result = await session.run(
                REQUEUE_FAILED_PROFILE_ANALYSIS_REQUEST,
                person_id=person_id,
                request_id=request_id,
                max_attempts=max_attempts,
            )
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        state = to_str(mapped.get("state"))
        if state == "request_not_found":
            return None
        valid_states = {
            "requeued",
            "not_terminal",
            "already_active",
            "nonrecoverable",
            "revision_conflict",
            "attempt_limited",
            "requeue_limited",
        }
        if state not in valid_states:
            raise ValueError("invalid profile analysis requeue state")
        return ProfileAnalysisRequestRequeueResult(
            request_id=to_str(mapped.get("request_id")),
            person_id=to_str(mapped.get("person_id")),
            analysis_type=cast(ProfileAnalysisType, to_str(mapped.get("analysis_type"))),
            state=cast(
                Literal[
                    "requeued",
                    "not_terminal",
                    "already_active",
                    "nonrecoverable",
                    "revision_conflict",
                    "attempt_limited",
                    "requeue_limited",
                ],
                state,
            ),
        )

    async def get_profile_analysis_history(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType | None,
        skip: int,
        limit: int,
    ) -> tuple[list[ProfileAnalysisHistoryItem], int] | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_PROFILE_ANALYSIS_HISTORY,
                person_id=person_id,
                analysis_type=analysis_type,
                skip=skip,
                limit=limit,
            )
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        raw_analyses = mapped.get("analyses")
        if not isinstance(raw_analyses, list):
            raise TypeError("profile analysis history query returned invalid analyses")
        items: list[ProfileAnalysisHistoryItem] = []
        for raw_analysis in raw_analyses:
            if not isinstance(raw_analysis, dict):
                raise TypeError("profile analysis history query returned invalid analysis")
            items.append(map_profile_analysis_history_item({"analysis": raw_analysis}))
        return items, to_total(record)

    async def get_source_records(
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_SOURCE_RECORDS,
                    person_id=person_id,
                    skip=skip,
                    limit=limit + 1,
                    entity_key=entity_key,
                    record_type=record_type,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(
                    COUNT_PERSON_SOURCE_RECORDS,
                    person_id=person_id,
                    entity_key=entity_key,
                    record_type=record_type,
                )
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_source_record(rec) for rec in records[:limit]], total

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]:
        async with get_session() as session:
            result = await session.run(GET_PERSON_SOURCE_RECORD_ENTITY_FACETS, person_id=person_id)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_source_record_entity_facet(rec) for rec in records]

    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_BANKRUPTCY_CASES,
                    person_id=person_id,
                    skip=skip,
                    limit=limit + 1,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(COUNT_PERSON_BANKRUPTCY_CASES, person_id=person_id)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_bankruptcy_case(rec) for rec in records[:limit]], total

    async def get_identifiers(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonIdentifier], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_IDENTIFIERS,
                    person_id=person_id,
                    skip=skip,
                    limit=limit + 1,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(COUNT_PERSON_IDENTIFIERS, person_id=person_id)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_person_identifier(rec) for rec in records[:limit]], total

    async def get_connections(
        self,
        person_id: str,
        connection_type: ConnectionType,
        identifier_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[PersonConnection], int]:
        query = _connection_query(connection_type)
        count_query = _connection_count_query(connection_type)

        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    query,
                    person_id=person_id,
                    identifier_type=identifier_type,
                    skip=skip,
                    limit=limit + 1,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(
                    count_query, person_id=person_id, identifier_type=identifier_type
                )
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_connection(rec) for rec in records[:limit]], total

    async def get_shared_identifier_candidates(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonSharedIdentifierCandidate], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_SHARED_IDENTIFIERS,
                    person_id=person_id,
                    skip=skip,
                    limit=limit + 1,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(COUNT_PERSON_SHARED_IDENTIFIERS, person_id=person_id)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        candidates = [map_shared_identifier_candidate(rec) for rec in records[:limit]]
        return candidates, total

    async def get_possible_match_detail(
        self, person_id: str, candidate_person_id: str
    ) -> PossibleMatchDetail | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_POSSIBLE_MATCH_DETAIL,
                person_id=person_id,
                candidate_person_id=candidate_person_id,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        if not records:
            return None
        return map_possible_match_detail(records)

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]:
        async with get_session() as session:
            result = await session.run(GET_PERSON_ENTITIES, person_id=person_id)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_person_entity(rec) for rec in records]

    async def get_loyalty(self, person_id: str) -> list[LoyaltySummary] | None:
        async with get_session() as session:
            result = await session.run(GET_PERSON_LOYALTY, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        return _map_loyalty(mapped.get("loyalty_rows"))

    async def get_vehicles(self, person_id: str) -> list[VehicleSummary] | None:
        async with get_session() as session:
            result = await session.run(GET_PERSON_VEHICLES, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        return _map_vehicles(mapped.get("vehicles"))

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None:
        query = get_graph_query(max_hops)
        async with get_session() as session:
            result = await session.run(query, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        return map_person_graph(record_to_dict(record.keys(), list(record.values())))

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        query = get_node_graph_query(max_hops)
        async with get_session() as session:
            result = await session.run(query, element_id=element_id)
            record = await result.single()
        if record is None:
            return None
        return map_person_graph(record_to_dict(record.keys(), list(record.values())))

    async def get_audit(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[AuditEvent], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_AUDIT,
                    person_id=person_id,
                    skip=skip,
                    limit=limit + 1,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(COUNT_PERSON_AUDIT, person_id=person_id)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_audit_event(rec) for rec in records[:limit]], total

    async def get_matches(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[MatchDecision], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_MATCHES,
                    person_id=person_id,
                    skip=skip,
                    limit=limit,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(COUNT_PERSON_MATCHES, person_id=person_id)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_match_decision(rec) for rec in records], total

    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]:
        async def _run_data() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    GET_PERSON_TIMELINE,
                    person_id=person_id,
                    skip=skip,
                    limit=limit + 1,
                )
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(COUNT_PERSON_TIMELINE, person_id=person_id)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_data(), _run_count())
        return [map_timeline_group(rec) for rec in records[:limit]], total

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_TIMELINE_TARGET,
                person_id=person_id,
                source_record_pk=source_record_pk,
            )
            record = await result.single()
        if record is None:
            return None
        return map_timeline_group(record_to_dict(record.keys(), list(record.values())))
