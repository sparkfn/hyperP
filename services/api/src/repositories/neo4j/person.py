"""Neo4j implementation of PersonRepository."""

from __future__ import annotations

import asyncio
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
    GET_PERSON_LIST_SUMMARY,
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
from src.repositories.protocols.person import PersonListFilters, PersonPage
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


class Neo4jPersonRepository:
    def __init__(self) -> None:
        self._summary_cache: PersonListSummary | None = None
        self._summary_cache_expires_at = 0.0
        self._summary_cache_lock = asyncio.Lock()

    def _cached_summary(self) -> PersonListSummary | None:
        if self._summary_cache is None or monotonic() >= self._summary_cache_expires_at:
            return None
        return PersonListSummary.model_validate(self._summary_cache.model_dump())

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

        async def _run_list() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(
                    list_query,
                    {**cypher_params, "skip": skip, "limit": limit + 1},
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

    async def _load_list_summary(self) -> PersonListSummary:
        async with get_session() as session:
            result = await session.run(GET_PERSON_LIST_SUMMARY)
            record = await result.single()
        if record is None:
            return PersonListSummary()
        values = record_to_dict(record.keys(), list(record.values()))
        return PersonListSummary(
            all_profiles_count=to_int(values.get("all_profiles_count")),
            high_risk_count=to_int(values.get("high_risk_count")),
            high_value_count=to_int(values.get("high_value_count")),
            no_contact_count=to_int(values.get("no_contact_count")),
        )

    async def get_list_summary(self) -> PersonListSummary:
        ttl = config.person_list_summary_cache_ttl_seconds
        if ttl <= 0:
            return await self._load_list_summary()

        cached = self._cached_summary()
        if cached is not None:
            return cached

        async with self._summary_cache_lock:
            cached = self._cached_summary()
            if cached is not None:
                return cached
            summary = await self._load_list_summary()
            self._summary_cache = summary
            self._summary_cache_expires_at = monotonic() + ttl
            return PersonListSummary.model_validate(summary.model_dump())

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        async with get_session() as session:
            result = await session.run(
                FIND_PERSON_BY_IDENTIFIER, identifier_type=identifier_type, value=value
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
