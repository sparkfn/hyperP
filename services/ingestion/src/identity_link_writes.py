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
from src.identifier_scopes import identifier_scope
from src.identity_links import (
    IdentifierKey,
    IdentityLinkConflictError,
    LinkRecomputeOutcome,
    MaterializationOutcome,
    MaterializationStatus,
    ParticipantEvidence,
    ParticipantKind,
    PromotionOutcome,
    build_link_rows,
    identifier_key_row,
    identifier_key_tuple,
    materializes,
    parse_field_trust,
    sorted_lock_rows,
)
from src.models import NormalizedIdentifier, RecordType
from src.normalizers.nric import is_nric_digest
from src.nric_anchor import NameDob, nric_anchor_gate


@dataclass(frozen=True)
class _ProfileState:
    status: str
    evidence_version: int
    promoted_person_id: str | None


@dataclass(frozen=True)
class _OpenProfile:
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
        _participant_evidence(row)
        for row in tx.run(
            queries.FETCH_PARTICIPANT_EVIDENCE,
            participant_kind=participant_kind,
            participant_id=participant_id,
        )
    ]
    evidence_keys = {identifier_key_tuple(row) for row in evidence}
    lock_identifiers(tx, [identifier_key_row(key) for key in sorted(evidence_keys)])
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
    _recompute_participant_counts(
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
) -> MaterializationOutcome:
    """Materialize, join, or park the Profile cluster containing one SourceRecord."""
    source_keys = _fetch_record_identifier_keys(tx, source_record_pk)
    if not source_keys:
        return MaterializationOutcome(status="no_keys", profile_id=None)
    lock_identifiers(tx, source_keys)
    open_profiles, evidence = _fetch_cluster(tx, source_keys, source_record_pk)
    cluster_keys = {identifier_key_tuple(row) for row in evidence}
    lock_identifiers(tx, [identifier_key_row(key) for key in sorted(cluster_keys)])
    if not materializes(evidence):
        tx.run(queries.SET_SOURCE_RECORD_PARKED, source_record_pk=source_record_pk)
        return MaterializationOutcome(status="parked", profile_id=None)
    member_pks = sorted({row["source_record_pk"] for row in evidence})
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


def promote_profile(
    tx: ManagedTransaction,
    *,
    profile_id: str,
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
    holder: NameDob | None = None,
) -> PromotionOutcome:
    """Promote an open Profile through the existing NRIC anchor gate.

    The caller supplies the record context; this function evaluates
    :func:`src.nric_anchor.nric_anchor_gate` itself, so no promotion can bypass
    it. Only a gate-approved HMAC digest becomes a Person ``nric_key``.
    """
    if identifier.identifier_type != "nric" or not is_nric_digest(identifier.normalized_value):
        return PromotionOutcome(status="rejected", profile_id=profile_id, reason="no_key")
    gate = nric_anchor_gate(identifier, source_key, record_type, incoming, holder)
    if not gate.promotes_person:
        return PromotionOutcome(status="rejected", profile_id=profile_id, reason=gate.reason)
    nric_key = identifier.normalized_value
    nric_scope = identifier_scope("nric", identifier.source_instance_id)
    nric_row = IdentifierKey(
        identifier_type="nric",
        identifier_scope=nric_scope,
        normalized_value=nric_key,
    )
    lock_identifiers(tx, [nric_row])
    state = _require_profile_state(tx, profile_id)
    if state.status == "promoted":
        person_row = tx.run(queries.FETCH_PERSON_BY_NRIC, nric_key=nric_key).single()
        person_id = None if person_row is None else _required_str(person_row, "person_id")
        if person_id is not None and person_id == state.promoted_person_id:
            return PromotionOutcome(
                status="already_promoted", profile_id=profile_id, person_id=person_id
            )
        raise IdentityLinkConflictError(f"profile {profile_id} is promoted to another Person")
    if state.status != "open":
        raise IdentityLinkConflictError(f"profile {profile_id} is not promotable")
    evidence = [
        _participant_evidence(row)
        for row in tx.run(
            queries.FETCH_PARTICIPANT_EVIDENCE,
            participant_kind="profile",
            participant_id=profile_id,
        )
    ]
    evidence_keys = {identifier_key_tuple(row) for row in evidence}
    lock_identifiers(
        tx,
        [
            identifier_key_row(key)
            for key in sorted(evidence_keys | {identifier_key_tuple(nric_row)})
        ],
    )
    person_id, person_created = _merge_person_by_nric(tx, nric_key, nric_scope)
    promoted = tx.run(
        queries.PROMOTE_PROFILE,
        profile_id=profile_id,
        expected_version=state.evidence_version,
        person_id=person_id,
    ).single()
    if promoted is None:
        raise IdentityLinkConflictError(f"profile {profile_id} changed during promotion")
    tx.run(
        queries.REATTRIBUTE_PROFILE_MEMBERS_TO_PERSON,
        profile_id=profile_id,
        person_id=person_id,
    ).consume()
    recompute_participant_links(tx, "person", person_id)
    _recompute_participant_counts(tx, {identifier_key_tuple(nric_row)})
    return PromotionOutcome(
        status="promoted",
        profile_id=profile_id,
        person_id=person_id,
        person_created=person_created,
    )


def _fetch_record_identifier_keys(
    tx: ManagedTransaction, source_record_pk: str
) -> list[IdentifierKey]:
    return [
        _key_row_from_record(row)
        for row in tx.run(queries.FETCH_RECORD_IDENTIFIER_KEYS, source_record_pk=source_record_pk)
    ]


def _open_profiles_for_keys(
    tx: ManagedTransaction, keys: Sequence[IdentifierKey]
) -> list[_OpenProfile]:
    return [
        _OpenProfile(
            profile_id=_required_str(row, "profile_id"),
            evidence_version=_required_int(row, "evidence_version"),
        )
        for row in tx.run(queries.FETCH_OPEN_PROFILES_FOR_IDENTIFIERS, identifier_keys=list(keys))
    ]


def _fetch_cluster(
    tx: ManagedTransaction,
    keys: Sequence[IdentifierKey],
    source_record_pk: str,
) -> tuple[list[_OpenProfile], list[ParticipantEvidence]]:
    """Read the locked cluster's open Profiles and full assertion evidence."""
    open_profiles = [
        _OpenProfile(
            profile_id=_required_str(row, "profile_id"),
            evidence_version=_required_int(row, "evidence_version"),
        )
        for row in tx.run(queries.FETCH_OPEN_PROFILES_FOR_IDENTIFIERS, identifier_keys=list(keys))
    ]
    evidence = [
        _participant_evidence(row)
        for row in tx.run(
            queries.FETCH_CLUSTER_EVIDENCE,
            identifier_keys=list(keys),
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
    return [_key_row_from_value(entry) for entry in raw]


def _recompute_participant_counts(
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
    loser: _OpenProfile,
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
    _recompute_participant_counts(tx, {identifier_key_tuple(row) for row in stale_keys})
    return _required_int(moved, "moved_count")


def _require_profile_state(tx: ManagedTransaction, profile_id: str) -> _ProfileState:
    row = tx.run(queries.FETCH_PROFILE_STATE, profile_id=profile_id).single()
    if row is None:
        raise IdentityLinkConflictError(f"profile {profile_id} does not exist")
    return _ProfileState(
        status=_required_str(row, "status"),
        evidence_version=_required_int(row, "evidence_version"),
        promoted_person_id=_optional_str(row, "promoted_person_id"),
    )


def _merge_person_by_nric(
    tx: ManagedTransaction,
    nric_key: str,
    nric_scope: str,
) -> tuple[str, bool]:
    row = tx.run(
        queries.MERGE_PERSON_BY_NRIC,
        nric_key=nric_key,
        identifier_scope=nric_scope,
    ).single()
    if row is None:
        raise IdentityLinkConflictError("NRIC Person anchor did not materialize")
    created: object = row["was_created"]
    return _required_str(row, "person_id"), created is True


def _participant_evidence(row: Record) -> ParticipantEvidence:
    identifier_type = _required_str(row, "identifier_type")
    field_trust = parse_field_trust(row["field_trust"])
    return ParticipantEvidence(
        source_record_pk=_required_str(row, "source_record_pk"),
        identifier_id=_required_str(row, "identifier_id"),
        identifier_type=identifier_type,
        identifier_scope=_required_str(row, "identifier_scope"),
        normalized_value=_required_str(row, "normalized_value"),
        quality_flag=_optional_str(row, "quality_flag"),
        is_verified=_optional_bool(row, "is_verified"),
        trust_tier=field_trust.get(identifier_type) if field_trust is not None else None,
        observed_at=_optional_str(row, "observed_at"),
    )


def _key_row_from_record(row: Record) -> IdentifierKey:
    return IdentifierKey(
        identifier_type=_required_str(row, "identifier_type"),
        identifier_scope=_required_str(row, "identifier_scope"),
        normalized_value=_required_str(row, "normalized_value"),
    )


def _key_row_from_value(value: object) -> IdentifierKey:
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


def _malformed(key: str) -> NoReturn:
    raise ValueError(f"{key} evidence is malformed")


def _required_str(row: Record, key: str) -> str:
    value: object = row[key]
    return value if isinstance(value, str) else _malformed(key)


def _optional_str(row: Record, key: str) -> str | None:
    value: object = row[key]
    return value if value is None or isinstance(value, str) else _malformed(key)


def _required_int(row: Record, key: str) -> int:
    value: object = row[key]
    return value if isinstance(value, int) and not isinstance(value, bool) else _malformed(key)


def _optional_bool(row: Record, key: str) -> bool | None:
    value: object = row[key]
    return value if value is None or isinstance(value, bool) else _malformed(key)


def anchor_person_by_nric(
    tx: ManagedTransaction,
    identifier: NormalizedIdentifier,
) -> tuple[str, bool]:
    """Anchor a Person by a gate-approved NRIC digest; returns ``(person_id, created)``."""
    nric_scope = identifier_scope("nric", identifier.source_instance_id)
    return _merge_person_by_nric(tx, identifier.normalized_value, nric_scope)


def anchor_existing_person(
    tx: ManagedTransaction,
    person_id: str,
    identifier: NormalizedIdentifier,
) -> bool:
    """Set a Person's NRIC anchor without replacing a different existing key.

    Returns ``True`` when the Person already carries a different ``nric_key``
    (a conflict to surface, never overwrite) and the anchor was not applied.
    """
    nric_scope = identifier_scope("nric", identifier.source_instance_id)
    row = tx.run(
        queries.ANCHOR_EXISTING_PERSON_BY_NRIC,
        person_id=person_id,
        nric_key=identifier.normalized_value,
        identifier_scope=nric_scope,
    ).single()
    return row is None


def promote_profiles_for_nric(
    tx: ManagedTransaction,
    identifier: NormalizedIdentifier,
    *,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
) -> tuple[str, ...]:
    """Promote every open Profile holding the NRIC identifier; returns promoted IDs."""
    nric_scope = identifier_scope("nric", identifier.source_instance_id)
    key = IdentifierKey(
        identifier_type="nric",
        identifier_scope=nric_scope,
        normalized_value=identifier.normalized_value,
    )
    promoted: list[str] = []
    for profile in _open_profiles_for_keys(tx, [key]):
        outcome = promote_profile(
            tx,
            profile_id=profile.profile_id,
            identifier=identifier,
            source_key=source_key,
            record_type=record_type,
            incoming=incoming,
        )
        if outcome.status == "promoted":
            promoted.append(profile.profile_id)
    return tuple(promoted)
