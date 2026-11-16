"""Writer layer for the derived C3 identity-link projection.

Projects immutable ``ASSERTS`` evidence into ``HAS_IDENTIFIER`` links, open
Profiles, and NRIC-anchored Persons. The pure aggregation helpers live in
:mod:`src.identity_links`; Cypher lives only in
:mod:`src.graph.queries.identity_links`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import NoReturn

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.identity_links import (
    MAX_RECORD_EXPANSION,
    IdentifierKey,
    IdentityLinkConflictError,
    LinkRecomputeOutcome,
    MaterializationOutcome,
    MaterializationStatus,
    ParticipantEvidence,
    ParticipantKind,
    build_link_rows,
    identifier_key_row,
    identifier_key_tuple,
    materializes,
    parse_field_trust,
    sorted_lock_rows,
)
from src.models import JsonValue
from src.pipeline_normalization import fanout_cap_for


@dataclass(frozen=True)
class OpenProfile:
    profile_id: str
    evidence_version: int


def lock_identifiers(tx: ManagedTransaction, keys: Iterable[IdentifierKey]) -> None:
    """Acquire Identifier write locks in canonical order with a value-preserving write."""
    rows = sorted_lock_rows(keys)
    if rows:
        tx.run(queries.LOCK_IDENTIFIERS, identifier_keys=rows)


def recompute_participant_links(
    tx: ManagedTransaction,
    participant_kind: ParticipantKind,
    participant_id: str,
) -> LinkRecomputeOutcome:
    """Recompute one participant's derived ``HAS_IDENTIFIER`` links from its evidence."""
    evidence = [
        participant_evidence(row)
        for row in tx.run(
            queries.FETCH_PARTICIPANT_EVIDENCE,
            participant_kind=participant_kind,
            participant_id=participant_id,
        )
    ]
    evidence_keys = {identifier_key_tuple(row) for row in evidence}
    links = build_link_rows(participant_id, evidence)
    link_ids = [str(row["link_id"]) for row in links]
    stale_keys = _delete_stale_links(tx, participant_kind, participant_id, link_ids)
    if links:
        tx.run(
            queries.MERGE_PARTICIPANT_HAS_IDENTIFIER_LINKS,
            participant_kind=participant_kind,
            participant_id=participant_id,
            links=links,
        )
    recompute_participant_counts(
        tx, evidence_keys | {identifier_key_tuple(row) for row in stale_keys}
    )
    return LinkRecomputeOutcome(
        participant_kind=participant_kind,
        participant_id=participant_id,
        link_count=len(links),
        stale_link_count=len(stale_keys),
    )


def materialize_profile_for_record(
    tx: ManagedTransaction,
    source_record_pk: str,
    *,
    locked_keys: frozenset[tuple[str, str, str]] | None = None,
) -> MaterializationOutcome:
    """Materialize, join, or park the Profile cluster containing one SourceRecord.

    The caller may pass the already-locked key closure (the pipeline locks one
    sorted neighbourhood batch before staging). Otherwise this function derives
    its closure with a key-only read and takes exactly one sorted lock batch. An
    evidence key outside the locked closure means a concurrent writer grew the
    cluster; the transaction is abandoned with :class:`IdentityLinkConflictError`.
    """
    source_keys = fetch_record_identifier_keys(tx, source_record_pk)
    if not source_keys:
        return MaterializationOutcome(status="no_keys", profile_id=None)
    if locked_keys is None:
        closure_keys = fetch_cluster_key_closure(tx, source_keys, source_record_pk)
        locked_keys = frozenset({identifier_key_tuple(key) for key in source_keys} | closure_keys)
        lock_identifiers(tx, [identifier_key_row(key) for key in sorted(locked_keys)])
    open_profiles, evidence = _fetch_cluster(tx, source_keys, source_record_pk)
    evidence_keys = {identifier_key_tuple(row) for row in evidence}
    if not evidence_keys <= set(locked_keys):
        raise IdentityLinkConflictError(
            f"cluster for record {source_record_pk} grew beyond its locked key closure"
        )
    member_pks = sorted({row["source_record_pk"] for row in evidence})
    if len(member_pks) > MAX_RECORD_EXPANSION:
        detached = tx.run(
            queries.DETACH_PROFILE_MEMBERS,
            source_record_pks=[source_record_pk],
            profile_id=None,
            expected_version=None,
        ).single()
        raw_profile_ids: object = None if detached is None else detached["profile_ids"]
        detached_profile_ids = (
            [value for value in raw_profile_ids if isinstance(value, str)]
            if isinstance(raw_profile_ids, list)
            else []
        )
        tx.run(queries.SET_SOURCE_RECORD_PARKED, source_record_pk=source_record_pk)
        for detached_profile_id in detached_profile_ids:
            recompute_participant_links(tx, "profile", detached_profile_id)
        recompute_participant_counts(tx, {identifier_key_tuple(key) for key in source_keys})
        return MaterializationOutcome(status="parked", profile_id=None)
    if not materializes(evidence):
        tx.run(queries.SET_SOURCE_RECORD_PARKED, source_record_pk=source_record_pk)
        return MaterializationOutcome(status="parked", profile_id=None)
    absorbed: list[str] = []
    if open_profiles:
        profile_id = open_profiles[0].profile_id
        evidence_version = open_profiles[0].evidence_version
        for loser in open_profiles[1:]:
            if _absorb_profile(tx, profile_id, evidence_version, loser) > 0:
                evidence_version += 1
            absorbed.append(loser.profile_id)
        status: MaterializationStatus = "joined"
    else:
        profile_id = str(uuid.uuid4())
        tx.run(queries.CREATE_PROFILE, profile_id=profile_id)
        evidence_version = 0
        status = "created"
    attached = tx.run(
        queries.ATTACH_PROFILE_MEMBERS,
        profile_id=profile_id,
        expected_version=evidence_version,
        source_record_pks=list(member_pks),
    ).single()
    if attached is None:
        raise IdentityLinkConflictError(f"profile {profile_id} changed during member attach")
    recompute_participant_links(tx, "profile", profile_id)
    return MaterializationOutcome(
        status=status,
        profile_id=profile_id,
        absorbed_profile_ids=tuple(absorbed),
        member_count=len(member_pks),
    )


def fetch_record_identifier_keys(
    tx: ManagedTransaction, source_record_pk: str
) -> list[IdentifierKey]:
    return [
        key_row_from_record(row)
        for row in tx.run(queries.FETCH_RECORD_IDENTIFIER_KEYS, source_record_pk=source_record_pk)
    ]


def cap_key_row(key: IdentifierKey) -> dict[str, JsonValue]:
    """Return one Identifier key row carrying its expansion cap.

    Registry types with a fanout cap keep it; uncapped types fall back to the
    record-expansion bound so one hub cannot grow a cluster without limit.
    """
    configured_cap = fanout_cap_for(key["identifier_type"])
    return {
        "identifier_type": key["identifier_type"],
        "identifier_scope": key["identifier_scope"],
        "normalized_value": key["normalized_value"],
        "fanout_cap": (configured_cap if configured_cap is not None else MAX_RECORD_EXPANSION),
    }


def open_profiles_for_keys(
    tx: ManagedTransaction, keys: Sequence[IdentifierKey]
) -> list[OpenProfile]:
    return [
        OpenProfile(
            profile_id=required_str(row, "profile_id"),
            evidence_version=required_int(row, "evidence_version"),
        )
        for row in tx.run(
            queries.FETCH_OPEN_PROFILES_FOR_IDENTIFIERS,
            identifier_keys=[cap_key_row(key) for key in keys],
        )
    ]


def fetch_cluster_key_closure(
    tx: ManagedTransaction,
    keys: Sequence[IdentifierKey],
    source_record_pk: str,
) -> set[tuple[str, str, str]]:
    """Derive the affected key closure before any lock is taken."""
    return {
        (
            required_str(row, "identifier_type"),
            required_str(row, "identifier_scope"),
            required_str(row, "normalized_value"),
        )
        for row in tx.run(
            queries.FETCH_CLUSTER_KEY_CLOSURE,
            identifier_keys=[cap_key_row(key) for key in keys],
            source_record_pk=source_record_pk,
        )
    }


def _fetch_cluster(
    tx: ManagedTransaction,
    keys: Sequence[IdentifierKey],
    source_record_pk: str,
) -> tuple[list[OpenProfile], list[ParticipantEvidence]]:
    """Read the locked cluster's open Profiles and full assertion evidence."""
    open_profiles = open_profiles_for_keys(tx, keys)
    evidence = [
        participant_evidence(row)
        for row in tx.run(
            queries.FETCH_CLUSTER_EVIDENCE,
            identifier_keys=[cap_key_row(key) for key in keys],
            source_record_pk=source_record_pk,
        )
    ]
    return open_profiles, evidence


def _delete_stale_links(
    tx: ManagedTransaction,
    participant_kind: ParticipantKind,
    participant_id: str,
    link_ids: Sequence[str],
) -> list[IdentifierKey]:
    row = tx.run(
        queries.DELETE_STALE_PARTICIPANT_HAS_IDENTIFIER_LINKS,
        participant_kind=participant_kind,
        participant_id=participant_id,
        link_ids=list(link_ids),
    ).single()
    if row is None:
        return []
    raw: object = row["stale_identifier_keys"]
    if not isinstance(raw, list):
        raise ValueError("stale identifier rows are malformed")
    return [key_row_from_value(entry) for entry in raw]


def recompute_participant_counts(
    tx: ManagedTransaction,
    keys: Iterable[tuple[str, str, str]],
) -> None:
    rows = [
        {
            "identifier_type": identifier_type,
            "identifier_scope": scope,
            "normalized_value": value,
        }
        for identifier_type, scope, value in sorted(set(keys))
    ]
    if rows:
        tx.run(queries.RECOMPUTE_IDENTIFIER_PARTICIPANT_COUNTS, identifier_keys=rows)


def _absorb_profile(
    tx: ManagedTransaction,
    keeper_id: str,
    keeper_expected_version: int,
    loser: OpenProfile,
) -> int:
    retired = tx.run(
        queries.RETIRE_PROFILE,
        profile_id=loser.profile_id,
        expected_version=loser.evidence_version,
    ).single()
    if retired is None:
        raise IdentityLinkConflictError(f"profile {loser.profile_id} changed during union")
    moved = tx.run(
        queries.MOVE_PROFILE_MEMBERS,
        loser_profile_id=loser.profile_id,
        keeper_profile_id=keeper_id,
        keeper_expected_version=keeper_expected_version,
    ).single()
    if moved is None:
        raise IdentityLinkConflictError(f"profile {keeper_id} changed during union")
    stale_keys = _delete_stale_links(tx, "profile", loser.profile_id, ())
    recompute_participant_counts(tx, {identifier_key_tuple(row) for row in stale_keys})
    return required_int(moved, "moved_count")


def participant_evidence(row: Record) -> ParticipantEvidence:
    identifier_type = required_str(row, "identifier_type")
    field_trust = parse_field_trust(row["field_trust"])
    return ParticipantEvidence(
        source_record_pk=required_str(row, "source_record_pk"),
        identifier_id=required_str(row, "identifier_id"),
        identifier_type=identifier_type,
        identifier_scope=required_str(row, "identifier_scope"),
        normalized_value=required_str(row, "normalized_value"),
        quality_flag=optional_str(row, "quality_flag"),
        is_verified=optional_bool(row, "is_verified"),
        trust_tier=field_trust.get(identifier_type) if field_trust is not None else None,
        observed_at=optional_str(row, "observed_at"),
    )


def key_row_from_record(row: Record) -> IdentifierKey:
    return IdentifierKey(
        identifier_type=required_str(row, "identifier_type"),
        identifier_scope=required_str(row, "identifier_scope"),
        normalized_value=required_str(row, "normalized_value"),
    )


def key_row_from_value(value: object) -> IdentifierKey:
    if not isinstance(value, dict):
        raise ValueError("identifier key row is malformed")
    identifier_type = value.get("identifier_type")
    scope = value.get("identifier_scope")
    normalized_value = value.get("normalized_value")
    if not (
        isinstance(identifier_type, str)
        and isinstance(scope, str)
        and isinstance(normalized_value, str)
    ):
        raise ValueError("identifier key row is malformed")
    return IdentifierKey(
        identifier_type=identifier_type,
        identifier_scope=scope,
        normalized_value=normalized_value,
    )


def malformed(key: str) -> NoReturn:
    raise ValueError(f"{key} evidence is malformed")


def required_str(row: Record, key: str) -> str:
    value: object = row[key]
    return value if isinstance(value, str) else malformed(key)


def optional_str(row: Record, key: str) -> str | None:
    value: object = row[key]
    return value if value is None or isinstance(value, str) else malformed(key)


def required_int(row: Record, key: str) -> int:
    value: object = row[key]
    return value if isinstance(value, int) and not isinstance(value, bool) else malformed(key)


def optional_bool(row: Record, key: str) -> bool | None:
    value: object = row[key]
    return value if value is None or isinstance(value, bool) else malformed(key)
