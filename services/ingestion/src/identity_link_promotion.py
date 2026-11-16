"""NRIC anchoring and one-way Profile promotion for the C3 projection.

Promotion re-attributes a Profile's members onto the anchored Person, then
projects each newly re-attributed member's own normalized payload through the
same evidence linker normal ingestion uses. Cypher lives in
:mod:`src.graph.queries.identity_links`; the derived-link writer stays in
:mod:`src.identity_link_writes`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.identifier_scopes import identifier_scope
from src.identity_link_writes import (
    lock_identifiers,
    open_profiles_for_keys,
    optional_str,
    participant_evidence,
    recompute_participant_counts,
    recompute_participant_links,
    required_int,
    required_str,
)
from src.identity_links import (
    IdentifierKey,
    IdentityLinkConflictError,
    ParticipantEvidence,
    PromotionOutcome,
    identifier_key_row,
    identifier_key_tuple,
)
from src.identity_registry import FACT_ASSERTION_ATTRIBUTES
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.models import (
    NormalizedAttribute,
    NormalizedIdentifier,
    RecordType,
)
from src.normalizers.nric import is_nric_digest
from src.nric_anchor import NameDob, nric_anchor_gate
from src.pipeline_writes import attach_person_evidence

type _KeyTuple = tuple[str, str, str]


@dataclass(frozen=True)
class _ProfileState:
    status: str
    evidence_version: int
    promoted_person_id: str | None


def promote_profile(
    tx: ManagedTransaction,
    *,
    profile_id: str,
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
    locked_keys: frozenset[_KeyTuple] | None = None,
) -> PromotionOutcome:
    """Promote an open Profile through the existing NRIC anchor gate.

    The caller supplies the record context; this function evaluates
    :func:`src.nric_anchor.nric_anchor_gate` for the incoming record and for
    every member's holder facts, so no promotion can bypass contradiction
    checks. Only a gate-approved HMAC digest becomes a Person ``nric_key``.
    """
    if identifier.identifier_type != "nric" or not is_nric_digest(identifier.normalized_value):
        return PromotionOutcome(status="rejected", profile_id=profile_id, reason="no_key")
    gate = nric_anchor_gate(identifier, source_key, record_type, incoming, None)
    if not gate.promotes_person:
        return PromotionOutcome(status="rejected", profile_id=profile_id, reason=gate.reason)
    nric_key = identifier.normalized_value
    nric_scope = identifier_scope("nric", identifier.source_instance_id)
    nric_row = IdentifierKey(
        identifier_type="nric",
        identifier_scope=nric_scope,
        normalized_value=nric_key,
    )
    locked = _lock_profile_closure(tx, profile_id, nric_row, locked_keys)
    state = _require_profile_state(tx, profile_id)
    if state.status == "promoted":
        person_row = tx.run(queries.FETCH_PERSON_BY_NRIC, nric_key=nric_key).single()
        person_id = None if person_row is None else required_str(person_row, "person_id")
        promoted_id = state.promoted_person_id
        if person_id is not None and promoted_id is not None:
            if _canonical_person_id(tx, person_id) == _canonical_person_id(tx, promoted_id):
                return PromotionOutcome(
                    status="already_promoted", profile_id=profile_id, person_id=person_id
                )
        raise IdentityLinkConflictError(f"profile {profile_id} is promoted to another Person")
    if state.status != "open":
        raise IdentityLinkConflictError(f"profile {profile_id} is not promotable")
    evidence = _profile_evidence(tx, profile_id)
    evidence_keys = {
        (row["identifier_type"], row["identifier_scope"], row["normalized_value"])
        for row in evidence
    }
    if not (evidence_keys | {identifier_key_tuple(nric_row)}) <= locked:
        raise IdentityLinkConflictError(f"profile {profile_id} grew beyond its locked key closure")
    conflict_reason = _member_conflict(
        tx, profile_id, identifier, source_key, record_type, incoming
    )
    if conflict_reason is not None:
        return PromotionOutcome(status="rejected", profile_id=profile_id, reason=conflict_reason)
    person_id, person_created = _merge_person_by_nric(tx, nric_key, nric_scope)
    promoted = tx.run(
        queries.PROMOTE_PROFILE,
        profile_id=profile_id,
        expected_version=state.evidence_version,
        person_id=person_id,
    ).single()
    if promoted is None:
        raise IdentityLinkConflictError(f"profile {profile_id} changed during promotion")
    reattributed = tx.run(
        queries.REATTRIBUTE_PROFILE_MEMBERS_TO_PERSON,
        profile_id=profile_id,
        person_id=person_id,
    ).single()
    _attach_member_evidence(tx, person_id, reattributed)
    recompute_participant_links(tx, "person", person_id)
    recompute_participant_counts(tx, {identifier_key_tuple(nric_row)})
    return PromotionOutcome(
        status="promoted",
        profile_id=profile_id,
        person_id=person_id,
        person_created=person_created,
    )


def promote_profiles_for_nric(
    tx: ManagedTransaction,
    identifier: NormalizedIdentifier,
    *,
    identifier_keys: Sequence[IdentifierKey],
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
    locked_keys: frozenset[_KeyTuple] | None = None,
) -> tuple[str, ...]:
    """Promote every open Profile sharing an eligible key with the record."""
    nric_scope = identifier_scope("nric", identifier.source_instance_id)
    key = IdentifierKey(
        identifier_type="nric",
        identifier_scope=nric_scope,
        normalized_value=identifier.normalized_value,
    )
    discovery: list[IdentifierKey] = [key]
    seen = {identifier_key_tuple(key)}
    for candidate_key in identifier_keys:
        parts = identifier_key_tuple(candidate_key)
        if parts not in seen:
            seen.add(parts)
            discovery.append(candidate_key)
    promoted: list[str] = []
    for profile in open_profiles_for_keys(tx, discovery):
        outcome = promote_profile(
            tx,
            profile_id=profile.profile_id,
            identifier=identifier,
            source_key=source_key,
            record_type=record_type,
            incoming=incoming,
            locked_keys=locked_keys,
        )
        if outcome.status == "promoted":
            promoted.append(profile.profile_id)
    return tuple(promoted)


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


def _lock_profile_closure(
    tx: ManagedTransaction,
    profile_id: str,
    nric_row: IdentifierKey,
    locked_keys: frozenset[_KeyTuple] | None,
) -> frozenset[_KeyTuple]:
    """Return the locked key closure, taking one sorted batch when standalone."""
    preliminary = {identifier_key_tuple(row) for row in _profile_evidence(tx, profile_id)} | {
        identifier_key_tuple(nric_row)
    }
    if locked_keys is None:
        lock_identifiers(tx, [identifier_key_row(key) for key in sorted(preliminary)])
        return frozenset(preliminary)
    if not preliminary <= locked_keys:
        raise IdentityLinkConflictError(f"profile {profile_id} grew beyond its locked key closure")
    return locked_keys


def _profile_evidence(tx: ManagedTransaction, profile_id: str) -> list[ParticipantEvidence]:
    return list(
        participant_evidence(row)
        for row in tx.run(
            queries.FETCH_PARTICIPANT_EVIDENCE,
            participant_kind="profile",
            participant_id=profile_id,
        )
    )


def _require_profile_state(tx: ManagedTransaction, profile_id: str) -> _ProfileState:
    row = tx.run(queries.FETCH_PROFILE_STATE, profile_id=profile_id).single()
    if row is None:
        raise IdentityLinkConflictError(f"profile {profile_id} does not exist")
    return _ProfileState(
        status=required_str(row, "status"),
        evidence_version=required_int(row, "evidence_version"),
        promoted_person_id=optional_str(row, "promoted_person_id"),
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
    return required_str(row, "person_id"), created is True


def _canonical_person_id(tx: ManagedTransaction, person_id: str) -> str:
    """Resolve a Person through MERGED_INTO lineage to its active survivor."""
    row = tx.run(queries.FETCH_CANONICAL_PERSON_ID, person_id=person_id).single()
    if row is None:
        raise IdentityLinkConflictError(f"person {person_id} does not exist")
    return required_str(row, "person_id")


def _member_conflict(
    tx: ManagedTransaction,
    profile_id: str,
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
) -> str | None:
    """Return a gate/conflict reason when the Profile contradicts this NRIC."""
    for row in tx.run(queries.FETCH_PROFILE_MEMBER_EVIDENCE, profile_id=profile_id):
        for value in _str_values(row["nric_values"]):
            if value != identifier.normalized_value:
                return "nric_conflict"
        holder = NameDob(
            full_name=next(iter(_str_values(row["full_names"])), None),
            dob=next(iter(_str_values(row["dobs"])), None),
        )
        gate = nric_anchor_gate(identifier, source_key, record_type, incoming, holder)
        if not gate.promotes_person:
            return gate.reason
    return None


def _attach_member_evidence(
    tx: ManagedTransaction,
    person_id: str,
    row: Record | None,
) -> None:
    """Project every newly re-attributed member's own payload onto the Person."""
    if row is None:
        return
    raw_members: object = row["reattributed_members"]
    if not isinstance(raw_members, list):
        raise ValueError("re-attributed member rows are malformed")
    for raw_member in raw_members:
        member = _member_row(raw_member)
        payload = _optional_str_value(member["normalized_payload"])
        attach_person_evidence(
            tx,
            person_id=person_id,
            source_record_pk=_required_str_value(member["source_record_pk"]),
            source_system_key=_required_str_value(member["source_key"]),
            observed_at=_optional_str_value(member["observed_at"]),
            addresses=_member_addresses(payload),
            attributes=_member_attributes(payload),
            source_trust_tier=None,
            confidence=None,
        )


def _member_row(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("re-attributed member row is malformed")
    return {str(key): item for key, item in value.items()}


def _required_str_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("re-attributed member value is malformed")
    return value


def _optional_str_value(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("re-attributed member value is malformed")


def _str_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _payload_items(payload: str | None, key: str) -> list[object]:
    if payload is None:
        return []
    try:
        parsed: object = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    items = parsed.get(key)
    return items if isinstance(items, list) else []


def _member_addresses(payload: str | None) -> list[NormalizedAddressModel]:
    addresses: list[NormalizedAddressModel] = []
    for item in _payload_items(payload, "addresses"):
        try:
            addresses.append(NormalizedAddressModel.model_validate(item))
        except (TypeError, ValueError):
            continue
    return addresses


def _member_attributes(payload: str | None) -> list[NormalizedAttribute]:
    attributes: list[NormalizedAttribute] = []
    for item in _payload_items(payload, "attributes"):
        try:
            attribute = NormalizedAttribute.model_validate(item)
        except (TypeError, ValueError):
            continue
        if attribute.attribute_name in FACT_ASSERTION_ATTRIBUTES:
            attributes.append(attribute)
    return attributes
