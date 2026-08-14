"""Pure lifecycle planning for qualified CRM stage-history observations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.stage_history_ingestion import (
    CLASSIFY_STAGE_HISTORY_OBSERVATION,
)
from src.stage_history_ingestion_models import (
    StageHistoryAssociationState,
    StageHistoryAuthorityState,
    StageHistoryIdentityHashState,
    StageHistoryOccurrence,
    StageHistoryRetryState,
    StageHistoryTerminalDisposition,
    StageHistoryValidObservation,
)


@dataclass(frozen=True, slots=True)
class StageHistoryLifecycleSnapshot:
    """Read-side classification that the atomic repository must revalidate."""

    identity_hash_state: StageHistoryIdentityHashState
    association_state: StageHistoryAssociationState
    authority_state: StageHistoryAuthorityState


class StageHistoryLifecycleReader(Protocol):
    """Source-free graph read surface used before the atomic write CAS."""

    def classify(
        self, observation: StageHistoryValidObservation
    ) -> StageHistoryLifecycleSnapshot: ...


class Neo4jStageHistoryLifecycleReader:
    """Read current variant and parent state; the writer rechecks it under locks."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def classify(self, observation: StageHistoryValidObservation) -> StageHistoryLifecycleSnapshot:
        def _read(tx: ManagedTransaction) -> StageHistoryLifecycleSnapshot:
            record = tx.run(
                CLASSIFY_STAGE_HISTORY_OBSERVATION,
                event_identity=observation.event_identity,
                canonical_hash=observation.canonical_hash,
                logical_parent_source_system=observation.logical_parent_source_system,
                logical_parent_source_record_id=(observation.logical_parent_source_record_id),
            ).single()
            if record is None:
                raise RuntimeError("stage-history lifecycle classification returned no row")
            return _snapshot_from_record(record)

        return self._client.execute_read(_read)


def build_lifecycle_occurrence(
    observation: StageHistoryValidObservation,
    snapshot: StageHistoryLifecycleSnapshot,
) -> StageHistoryOccurrence:
    """Translate a read snapshot into the immutable unit disposition plan."""
    disposition, retry_state = _disposition(snapshot)
    return StageHistoryOccurrence(
        observation=observation,
        disposition=disposition,
        parse_scope="in_scope",
        identity_hash_state=snapshot.identity_hash_state,
        association_state=snapshot.association_state,
        authority_state=snapshot.authority_state,
        retry_state=retry_state,
    )


def _disposition(
    snapshot: StageHistoryLifecycleSnapshot,
) -> tuple[StageHistoryTerminalDisposition, StageHistoryRetryState]:
    if snapshot.identity_hash_state == "existing_same_hash":
        return "same_hash_replay", "none"
    if snapshot.identity_hash_state == "new_conflict_variant":
        if snapshot.authority_state != "withheld_conflict":
            raise ValueError("a differing hash must withhold conflicted authority")
        return "differing_hash_conflict", "none"
    state = snapshot.association_state
    authority = snapshot.authority_state
    if state == "selected_active" and authority == "effective":
        return "canonical_effective", "none"
    if state == "selected_pending_review" and authority == "withheld_parent":
        return "canonical_pending_parent", "pending"
    if state == "waiting" and authority == "withheld_parent":
        return "parent_waiting", "pending"
    if state == "ambiguous" and authority == "withheld_conflict":
        return "parent_ambiguous", "pending"
    raise ValueError("stage-history lifecycle snapshot is not executable")


def _snapshot_from_record(record: Record) -> StageHistoryLifecycleSnapshot:
    exact_count = _record_count(record, "exact_count")
    variant_count = _record_count(record, "variant_count")
    if exact_count > 1 or exact_count > variant_count:
        raise RuntimeError("stage-history variant classification is inconsistent")
    association = _association_state(record.get("association_state"))
    current_authority = _optional_authority_state(record.get("current_authority_state"))
    if exact_count == 1:
        if current_authority is None:
            raise RuntimeError("known stage-history variant lacks an authority head")
        return StageHistoryLifecycleSnapshot("existing_same_hash", association, current_authority)
    if variant_count:
        return StageHistoryLifecycleSnapshot(
            "new_conflict_variant", association, "withheld_conflict"
        )
    authority: StageHistoryAuthorityState
    if association == "selected_active":
        authority = "effective"
    elif association == "ambiguous":
        authority = "withheld_conflict"
    else:
        authority = "withheld_parent"
    return StageHistoryLifecycleSnapshot("new_variant", association, authority)


def _record_count(record: Record, key: str) -> int:
    value: object = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"stage-history lifecycle returned an invalid {key}")
    return value


def _association_state(value: object) -> StageHistoryAssociationState:
    if value == "selected_active":
        return "selected_active"
    if value == "selected_pending_review":
        return "selected_pending_review"
    if value == "waiting":
        return "waiting"
    if value == "ambiguous":
        return "ambiguous"
    raise RuntimeError("stage-history lifecycle returned an invalid parent state")


def _optional_authority_state(value: object) -> StageHistoryAuthorityState | None:
    if value is None:
        return None
    if value == "effective":
        return "effective"
    if value == "withheld_parent":
        return "withheld_parent"
    if value == "withheld_conflict":
        return "withheld_conflict"
    if value == "rejected":
        return "rejected"
    if value == "corrected":
        return "corrected"
    raise RuntimeError("stage-history lifecycle returned an invalid authority state")
