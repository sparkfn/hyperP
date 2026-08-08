"""Repository access for the durable Bitrix CRM deal-owner scope index."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from neo4j import ManagedTransaction, Record

from src.bitrix_ingestion_models import DealScopeState, FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.queries.bitrix_deal_scope import (
    GET_CURRENT_DEAL_SCOPE_BATCH,
    UPSERT_DEAL_SCOPE_MEMBERSHIPS,
)

MAX_DEAL_SCOPE_BATCH_SIZE = 250
DealScopeLookupState = Literal["in_scope", "out_of_scope", "indeterminate", "missing"]


@dataclass(frozen=True)
class CurrentDealScope:
    """Current authorization state for one logical Bitrix CRM deal."""

    deal_id: str
    scope_sequence: int
    scope_state: DealScopeState
    entity_key: str | None
    category_id: str | None
    source_record_pk: str | None


@dataclass(frozen=True)
class DealScopeObservation:
    """One durable scope observation from a bounded deal census batch.

    The current state is mutable for fast authorization. A new immutable
    ``CrmDealScopeMembership`` lineage node is created only when the semantic
    tuple ``(scope_state, entity_key, category_id)`` changes.
    """

    deal_id: str
    scope_state: DealScopeState
    category_id: str | None = None
    entity_key: str | None = None
    source_record_pk: str | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.deal_id, "deal_id")
        if self.scope_state not in {"in_scope", "out_of_scope", "indeterminate"}:
            raise ValueError("Invalid Bitrix deal scope state")
        _require_optional_identifier(self.category_id, "category_id")
        _require_optional_identifier(self.entity_key, "entity_key")
        _require_optional_identifier(self.source_record_pk, "source_record_pk")
        if self.scope_state == "in_scope":
            if self.category_id is None or self.entity_key is None:
                raise ValueError("In-scope deal observations require category_id and entity_key")
        elif self.entity_key is not None:
            raise ValueError("Only in-scope deal observations may have an entity_key")


@dataclass(frozen=True)
class DealScopeLookup:
    """Explicit result for one requested owner ID in a bounded lookup."""

    deal_id: str
    state: DealScopeLookupState
    current: CurrentDealScope | None

    def __post_init__(self) -> None:
        _require_identifier(self.deal_id, "deal_id")
        if self.current is None:
            if self.state != "missing":
                raise ValueError("Missing deal scope lookups cannot have a scope state")
            return
        if self.state != self.current.scope_state:
            raise ValueError("Deal scope lookup state must match its current scope")


class BitrixDealScopeRepository:
    """Persist and retrieve current deal scope without task-local maps.

    Mutations supplied with a split-stream fence acquire and validate that
    stream lock in the same transaction as the scope write.
    """

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def record_batch(
        self,
        observations: Sequence[DealScopeObservation],
        *,
        fence_context: FenceContext | None = None,
    ) -> dict[str, CurrentDealScope]:
        """Record up to ``MAX_DEAL_SCOPE_BATCH_SIZE`` distinct scope observations."""
        _validate_observations(observations)
        if not observations:
            return {}
        params = [
            {
                "deal_id": observation.deal_id,
                "scope_state": observation.scope_state,
                "category_id": observation.category_id,
                "entity_key": observation.entity_key,
                "source_record_pk": observation.source_record_pk,
            }
            for observation in observations
        ]

        def _work(tx: ManagedTransaction) -> dict[str, CurrentDealScope]:
            if fence_context is not None:
                assert_active_bitrix_fence(tx, fence_context)
            records = tx.run(
                UPSERT_DEAL_SCOPE_MEMBERSHIPS,
                source_key="bitrix_chat",
                observations=params,
            )
            result: dict[str, CurrentDealScope] = {}
            for record in records:
                current = _current_scope_from_record(record)
                if current.deal_id in result:
                    raise ValueError("Bitrix deal scope batch write returned duplicate deal IDs")
                result[current.deal_id] = current
            expected_deal_ids = {observation.deal_id for observation in observations}
            if set(result) != expected_deal_ids:
                raise ValueError(
                    "Bitrix deal scope batch write did not return every requested deal"
                )
            return result

        return self._client.execute_write(_work)

    def record_in_scope(
        self,
        *,
        deal_id: str,
        category_id: str,
        entity_key: str,
        source_record_pk: str,
        fence_context: FenceContext | None = None,
    ) -> CurrentDealScope:
        """Compatibility helper for a single in-scope observation."""
        observation = DealScopeObservation(
            deal_id=deal_id,
            scope_state="in_scope",
            category_id=category_id,
            entity_key=entity_key,
            source_record_pk=source_record_pk,
        )
        return self.record_batch([observation], fence_context=fence_context)[deal_id]

    def record_out_of_scope(
        self,
        *,
        deal_id: str,
        category_id: str | None,
        source_record_pk: str | None = None,
        fence_context: FenceContext | None = None,
    ) -> CurrentDealScope:
        """Record a deliberate current out-of-scope classification."""
        observation = DealScopeObservation(
            deal_id=deal_id,
            scope_state="out_of_scope",
            category_id=category_id,
            source_record_pk=source_record_pk,
        )
        return self.record_batch([observation], fence_context=fence_context)[deal_id]

    def record_indeterminate(
        self,
        *,
        deal_id: str,
        category_id: str | None = None,
        source_record_pk: str | None = None,
        fence_context: FenceContext | None = None,
    ) -> CurrentDealScope:
        """Record an unresolved state without authorizing activity persistence."""
        observation = DealScopeObservation(
            deal_id=deal_id,
            scope_state="indeterminate",
            category_id=category_id,
            source_record_pk=source_record_pk,
        )
        return self.record_batch([observation], fence_context=fence_context)[deal_id]

    def get_current_batch(
        self,
        deal_ids: Sequence[str],
        *,
        fence_context: FenceContext | None = None,
    ) -> dict[str, DealScopeLookup]:
        """Resolve up to ``MAX_DEAL_SCOPE_BATCH_SIZE`` owner IDs explicitly.

        Every requested ID appears in the returned mapping. Unknown IDs return
        ``state='missing'``; known IDs return their durable ``in_scope``,
        ``out_of_scope``, or ``indeterminate`` state.
        """
        _validate_deal_ids(deal_ids)
        _reserve_fence_context(fence_context)
        if not deal_ids:
            return {}

        def _work(tx: ManagedTransaction) -> dict[str, DealScopeLookup]:
            records = tx.run(
                GET_CURRENT_DEAL_SCOPE_BATCH,
                source_key="bitrix_chat",
                deal_ids=list(deal_ids),
            )
            result: dict[str, DealScopeLookup] = {}
            for record in records:
                deal_id = _required_str(record, "deal_id")
                if deal_id in result:
                    raise ValueError("Bitrix deal scope batch lookup returned duplicate deal IDs")
                if record["scope_state"] is None:
                    result[deal_id] = DealScopeLookup(
                        deal_id=deal_id, state="missing", current=None
                    )
                else:
                    current = _current_scope_from_record(record)
                    result[deal_id] = DealScopeLookup(
                        deal_id=deal_id,
                        state=current.scope_state,
                        current=current,
                    )
            expected_deal_ids = set(deal_ids)
            if set(result) != expected_deal_ids:
                raise ValueError(
                    "Bitrix deal scope batch lookup did not return every requested deal"
                )
            return result

        return self._client.execute_read(_work)

    def get_current(
        self,
        deal_id: str,
        *,
        fence_context: FenceContext | None = None,
    ) -> CurrentDealScope | None:
        """Compatibility helper that returns ``None`` for an unknown deal."""
        lookup = self.get_current_batch([deal_id], fence_context=fence_context)[deal_id]
        return lookup.current


def _validate_observations(observations: Sequence[DealScopeObservation]) -> None:
    if len(observations) > MAX_DEAL_SCOPE_BATCH_SIZE:
        raise ValueError(
            f"Bitrix deal scope batch exceeds {MAX_DEAL_SCOPE_BATCH_SIZE} observations"
        )
    deal_ids = [observation.deal_id for observation in observations]
    if len(set(deal_ids)) != len(deal_ids):
        raise ValueError("Bitrix deal scope batches require distinct deal IDs")


def _validate_deal_ids(deal_ids: Sequence[str]) -> None:
    if len(deal_ids) > MAX_DEAL_SCOPE_BATCH_SIZE:
        raise ValueError(f"Bitrix deal scope batch exceeds {MAX_DEAL_SCOPE_BATCH_SIZE} deal IDs")
    for deal_id in deal_ids:
        _require_identifier(deal_id, "deal_id")
    if len(set(deal_ids)) != len(deal_ids):
        raise ValueError("Bitrix deal scope lookups require distinct deal IDs")


def _reserve_fence_context(fence_context: FenceContext | None) -> None:
    """Reads accept a fence for call-site symmetry but do not acquire a write lock."""
    _ = fence_context


def _current_scope_from_record(record: Record) -> CurrentDealScope:
    scope_state = _scope_state(record)
    sequence = record["scope_sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("Invalid Bitrix deal scope sequence")
    return CurrentDealScope(
        deal_id=_required_str(record, "deal_id"),
        scope_sequence=sequence,
        scope_state=scope_state,
        entity_key=_optional_str(record, "entity_key"),
        category_id=_optional_str(record, "category_id"),
        source_record_pk=_optional_str(record, "source_record_pk"),
    )


def _require_identifier(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must be non-empty")


def _require_optional_identifier(value: str | None, name: str) -> None:
    if value is not None:
        _require_identifier(value, name)


def _scope_state(record: Record) -> DealScopeState:
    value = _required_str(record, "scope_state")
    if value == "in_scope":
        return "in_scope"
    if value == "out_of_scope":
        return "out_of_scope"
    if value == "indeterminate":
        return "indeterminate"
    raise ValueError("Invalid Bitrix deal scope state")


def _required_str(record: Record, key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"Invalid Bitrix deal scope {key}")
    return value


def _optional_str(record: Record, key: str) -> str | None:
    value: object = record[key]
    return value if isinstance(value, str) and value else None
