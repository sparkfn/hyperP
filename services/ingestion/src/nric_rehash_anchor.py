"""Bounded reads and writes for anchoring existing Persons by NRIC."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from functools import partial

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.nric_rehash_anchor import NRIC_REHASH_ANCHOR_PAGE
from src.identity_link_member_evidence import person_holder_evidence
from src.identity_link_promotion import anchor_existing_person
from src.identity_link_writes import lock_identifiers
from src.identity_links import IdentifierKey, identifier_key_tuple
from src.models import NormalizedIdentifier, QualityFlag
from src.nric_rehash_anchor_decide import (
    AnchorDecision,
    AnchorEvidence,
    AnchorIdentifier,
    AnchorSourceRecord,
    decide_anchor,
)
from src.nric_rehash_handoff import HandoffEntry
from src.nric_rehash_plan import NricRehashError
from src.nric_rehash_state import (
    MigrationState,
    advance_anchor_batch,
    fence_batch,
)


@dataclass(frozen=True)
class AnchorScan:
    handoff_entries: tuple[HandoffEntry, ...]
    already_anchored: int
    anchorable_remaining: int


@dataclass(frozen=True)
class _AnchorBatchOutcome:
    cursor: str | None
    row_count: int
    anchored: int


def read_anchor_page(
    tx: ManagedTransaction,
    after_person_id: str | None,
    batch_size: int,
) -> tuple[AnchorEvidence, ...]:
    """Read one bounded page of active Persons and their NRIC evidence."""
    return tuple(
        _anchor_evidence_from_record(record)
        for record in tx.run(
            NRIC_REHASH_ANCHOR_PAGE,
            after_person_id=after_person_id,
            batch_size=batch_size,
        )
    )


def scan_anchor_state(
    client: Neo4jClient,
    batch_size: int,
    key_version: str,
) -> AnchorScan:
    """Read every Person's anchor decision without writing graph state."""
    cursor: str | None = None
    handoffs: list[HandoffEntry] = []
    already_anchored = 0
    anchorable_remaining = 0
    while True:
        decisions = client.execute_read(
            partial(
                _read_anchor_decision_page,
                cursor=cursor,
                batch_size=batch_size,
                key_version=key_version,
            )
        )
        for evidence, decision in decisions:
            if decision.kind == "handoff":
                handoffs.append(_handoff_entry(evidence, decision))
            elif decision.kind == "already_anchored":
                already_anchored += 1
            else:
                anchorable_remaining += 1
        if not decisions or len(decisions) < batch_size:
            return AnchorScan(tuple(handoffs), already_anchored, anchorable_remaining)
        cursor = decisions[-1][0].person_id


def _read_anchor_decision_page(
    tx: ManagedTransaction,
    cursor: str | None,
    batch_size: int,
    key_version: str,
) -> tuple[tuple[AnchorEvidence, AnchorDecision], ...]:
    people = read_anchor_page(tx, cursor, batch_size)
    _validate_anchor_page(people, cursor)
    return tuple((evidence, _decision_for_person(tx, evidence, key_version)) for evidence in people)


def run_anchor_pass(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key_version: str,
) -> MigrationState:
    """Resume bounded, fenced writes that anchor approved existing Persons."""
    while True:
        outcome = _process_anchor_batch(client, state, owner, batch_size, key_version)
        if outcome.row_count == 0:
            return state
        state = replace(state, anchor_cursor=outcome.cursor, anchored=outcome.anchored)
        if outcome.row_count < batch_size:
            return state


def _process_anchor_batch(
    client: Neo4jClient,
    state: MigrationState,
    owner: str,
    batch_size: int,
    key_version: str,
) -> _AnchorBatchOutcome:
    def work(tx: ManagedTransaction) -> _AnchorBatchOutcome:
        fence_batch(tx, owner)
        people = read_anchor_page(tx, state.anchor_cursor, batch_size)
        _validate_anchor_page(people, state.anchor_cursor)
        if not people:
            return _AnchorBatchOutcome(state.anchor_cursor, 0, state.anchored)
        people, decisions = _lock_and_refresh_anchor_page(
            tx,
            people,
            state.anchor_cursor,
            batch_size,
            key_version,
        )
        approved = tuple(
            (evidence, decision) for evidence, decision in decisions if decision.kind == "anchor"
        )
        anchored_in_batch = 0
        for evidence, _decision in approved:
            if anchor_existing_person(
                tx, evidence.person_id, _normalized_identifier(evidence, key_version)
            ):
                raise NricRehashError("drift_detected")
            anchored_in_batch += 1
        cursor = people[-1].person_id
        anchored = advance_anchor_batch(tx, owner, cursor, anchored_in_batch)
        return _AnchorBatchOutcome(cursor, len(people), anchored)

    return client.execute_write(work)


def _lock_and_refresh_anchor_page(
    tx: ManagedTransaction,
    people: tuple[AnchorEvidence, ...],
    cursor: str | None,
    batch_size: int,
    key_version: str,
) -> tuple[tuple[AnchorEvidence, ...], tuple[tuple[AnchorEvidence, AnchorDecision], ...]]:
    initial_decisions = tuple(
        (evidence, _decision_for_person(tx, evidence, key_version)) for evidence in people
    )
    keys_by_tuple: dict[tuple[str, str, str], IdentifierKey] = {}
    for evidence, decision in initial_decisions:
        if decision.kind == "anchor":
            key = _identifier_key(evidence)
            keys_by_tuple[identifier_key_tuple(key)] = key
    if not keys_by_tuple:
        return people, initial_decisions

    locked_keys = set(keys_by_tuple)
    lock_identifiers(tx, [keys_by_tuple[key] for key in sorted(locked_keys)])
    refreshed = read_anchor_page(tx, cursor, batch_size)
    _validate_anchor_page(refreshed, cursor)
    if tuple(item.person_id for item in refreshed) != tuple(item.person_id for item in people):
        raise NricRehashError("drift_detected")
    refreshed_decisions = tuple(
        (evidence, _decision_for_person(tx, evidence, key_version)) for evidence in refreshed
    )
    current_keys = {
        identifier_key_tuple(_identifier_key(evidence))
        for evidence, decision in refreshed_decisions
        if decision.kind == "anchor"
    }
    if not current_keys <= locked_keys:
        raise NricRehashError("drift_detected")
    return refreshed, refreshed_decisions


def _validate_anchor_page(people: tuple[AnchorEvidence, ...], cursor: str | None) -> None:
    previous_id = cursor
    for evidence in people:
        if previous_id is not None and evidence.person_id <= previous_id:
            raise NricRehashError("paging_violation")
        previous_id = evidence.person_id


def _decision_for_person(
    tx: ManagedTransaction,
    evidence: AnchorEvidence,
    key_version: str,
) -> AnchorDecision:
    holders = person_holder_evidence(tx, evidence.person_id)
    return decide_anchor(evidence, key_version, holders)


def _identifier_key(evidence: AnchorEvidence) -> IdentifierKey:
    if len(evidence.identifiers) != 1:
        raise NricRehashError("drift_detected")
    identifier = evidence.identifiers[0]
    if identifier.normalized_value is None:
        raise NricRehashError("drift_detected")
    return IdentifierKey(
        identifier_type="nric",
        identifier_scope="global",
        normalized_value=identifier.normalized_value,
    )


def _normalized_identifier(evidence: AnchorEvidence, key_version: str) -> NormalizedIdentifier:
    if len(evidence.identifiers) != 1:
        raise NricRehashError("drift_detected")
    identifier = evidence.identifiers[0]
    if identifier.normalized_value is None or identifier.checksum_valid is not True:
        raise NricRehashError("drift_detected")
    return NormalizedIdentifier(
        identifier_type="nric",
        normalized_value=identifier.normalized_value,
        quality_flag=QualityFlag.VALID,
        checksum_valid=True,
        nric_key_version=key_version,
    )


def _handoff_entry(evidence: AnchorEvidence, decision: AnchorDecision) -> HandoffEntry:
    return HandoffEntry(
        person_id=evidence.person_id,
        reason=decision.reason,
        identifier_ids=tuple(
            sorted(identifier.identifier_id for identifier in evidence.identifiers)
        ),
        source_record_pks=tuple(
            sorted(record.source_record_pk for record in evidence.records)[:50]
        ),
    )


def _anchor_evidence_from_record(record: Record) -> AnchorEvidence:
    person_id = _required_string(record, "person_id")
    nric_key = _optional_string(record, "nric_key")
    has_anchor = _required_bool(record, "has_anchor")
    raw_identifiers: object = record.get("identifier_evidence")
    raw_records: object = record.get("records")
    evidence_truncated = _required_bool(record, "evidence_truncated")
    if not isinstance(raw_identifiers, list) or not isinstance(raw_records, list):
        raise NricRehashError("neo4j_error")
    return AnchorEvidence(
        person_id=person_id,
        nric_key=nric_key,
        has_anchor=has_anchor,
        identifiers=tuple(_identifier_from_value(item) for item in raw_identifiers),
        records=tuple(_source_record_from_value(item) for item in raw_records),
        evidence_truncated=evidence_truncated,
    )


def _identifier_from_value(value: object) -> AnchorIdentifier:
    if not isinstance(value, Mapping):
        raise NricRehashError("neo4j_error")
    identifier_id = _required_string(value, "identifier_id")
    normalized_value = _optional_string(value, "normalized_value")
    checksum_valid: object = value.get("checksum_valid")
    if checksum_valid is not None and not isinstance(checksum_valid, bool):
        raise NricRehashError("neo4j_error")
    return AnchorIdentifier(
        identifier_id=identifier_id,
        normalized_value=normalized_value,
        checksum_valid=checksum_valid,
        other_key_holders=_required_count(value, "other_key_holders"),
        other_active_evidence=_required_count(value, "other_active_evidence"),
    )


def _source_record_from_value(value: object) -> AnchorSourceRecord:
    if not isinstance(value, Mapping):
        raise NricRehashError("neo4j_error")
    return AnchorSourceRecord(
        source_record_pk=_required_string(value, "source_record_pk"),
        source_key=_optional_string(value, "source_key"),
        record_type=_optional_string(value, "record_type"),
        normalized_payload=_optional_string(value, "normalized_payload"),
    )


def _required_string(value: Record | Mapping[str, object], key: str) -> str:
    raw: object = value.get(key)
    if isinstance(raw, str) and raw:
        return raw
    raise NricRehashError("neo4j_error")


def _optional_string(value: Record | Mapping[str, object], key: str) -> str | None:
    raw: object = value.get(key)
    if raw is None or isinstance(raw, str):
        return raw
    raise NricRehashError("neo4j_error")


def _required_bool(value: Record, key: str) -> bool:
    raw: object = value.get(key)
    if isinstance(raw, bool):
        return raw
    raise NricRehashError("neo4j_error")


def _required_count(value: Mapping[str, object], key: str) -> int:
    raw: object = value.get(key)
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    raise NricRehashError("neo4j_error")
