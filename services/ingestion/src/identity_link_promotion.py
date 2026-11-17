"""NRIC anchoring and one-way Profile promotion for the C3 projection.

Promotion re-attributes a Profile's members onto the anchored Person, then
projects each newly re-attributed member's own normalized payload through the
same evidence linker normal ingestion uses. Cypher lives in
:mod:`src.graph.queries.identity_links`; the derived-link writer stays in
:mod:`src.identity_link_writes`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.identifier_scopes import identifier_scope
from src.identity_link_member_evidence import (
    MemberEvidence,
    attach_member_evidence,
    classify_members,
    cross_member_contradiction,
    member_dispositions,
    member_evidence_rows,
    person_holder_evidence,
    str_values,
    supersede_rows,
)
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
from src.models import NormalizedIdentifier, RecordType
from src.normalizers.nric import is_nric_digest
from src.nric_anchor import NameDob, nric_anchor_gate

type _KeyTuple = tuple[str, str, str]


@dataclass(frozen=True)
class NricPromotionContradiction:
    """An NRIC promotion rejected against a durable holder Person."""

    profile_id: str
    holder_person_id: str | None


@dataclass(frozen=True)
class NricPromotionOutcome:
    """Promoted profile ids plus durable-holder contradictions."""

    promoted_profile_ids: tuple[str, ...]
    contradictions: tuple[NricPromotionContradiction, ...]


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
    prior_promoted: Sequence[MemberEvidence] = (),
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
    members = member_evidence_rows(tx, profile_id)
    dispositions = member_dispositions(tx, profile_id, members)
    disposition_by_pk = {item.source_record_pk: item for item in dispositions}
    activate_members = [
        member
        for member in members
        if disposition_by_pk[member.source_record_pk].disposition == "activate"
    ]
    classification = classify_members(
        activate_members,
        identifier,
        source_key,
        record_type,
        incoming,
        prior_promoted,
    )
    if classification.rejection is not None:
        return PromotionOutcome(
            status="rejected", profile_id=profile_id, reason=classification.rejection
        )
    supersede_entries = supersede_rows(dispositions)
    reopen_rows = [item.source_record_pk for item in dispositions if item.disposition == "reopen"]
    if not classification.eligible:
        return _reject_with_dispositions(
            tx,
            profile_id=profile_id,
            expected_version=state.evidence_version,
            supersede_entries=supersede_entries,
            reopen_pks=reopen_rows,
            count_keys=evidence_keys | {identifier_key_tuple(nric_row)},
        )
    activate_pks = [member.source_record_pk for member in classification.eligible]
    reopen_pks = [*reopen_rows, *classification.excluded]
    person_id, person_created = _merge_person_by_nric(tx, nric_key, nric_scope)
    holder_evidence = person_holder_evidence(tx, person_id)
    if holder_evidence and cross_member_contradiction(
        [*classification.eligible, *prior_promoted],
        identifier,
        source_key,
        record_type,
        durable_holders=holder_evidence,
    ):
        return PromotionOutcome(
            status="rejected", profile_id=profile_id, reason="nric_contradiction"
        )
    promoted = tx.run(
        queries.PROMOTE_PROFILE,
        profile_id=profile_id,
        expected_version=state.evidence_version,
        person_id=person_id,
    ).single()
    if promoted is None:
        raise IdentityLinkConflictError(f"profile {profile_id} changed during promotion")
    if supersede_entries:
        _supersede(tx, supersede_entries)
    if reopen_pks:
        tx.run(
            queries.REOPEN_PROFILE_MEMBERS,
            profile_id=profile_id,
            source_record_pks=reopen_pks,
        )
    reattributed = tx.run(
        queries.REATTRIBUTE_PROFILE_MEMBERS_TO_PERSON,
        profile_id=profile_id,
        person_id=person_id,
        source_record_pks=activate_pks,
    ).single()
    activated_pks = set(_activated_pks(reattributed))
    missing_pks = [
        source_record_pk
        for source_record_pk in activate_pks
        if source_record_pk not in activated_pks
    ]
    if missing_pks:
        tx.run(
            queries.REOPEN_PROFILE_MEMBERS,
            profile_id=profile_id,
            source_record_pks=missing_pks,
        )
    attach_member_evidence(tx, person_id, reattributed)
    recompute_participant_links(tx, "person", person_id)
    recompute_participant_counts(tx, evidence_keys | {identifier_key_tuple(nric_row)})
    return PromotionOutcome(
        status="promoted",
        profile_id=profile_id,
        person_id=person_id,
        person_created=person_created,
        promoted_members=tuple(
            member for member in classification.eligible if member.source_record_pk in activated_pks
        ),
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
) -> NricPromotionOutcome:
    """Promote every open Profile sharing an eligible key with the record.

    Returns promoted profile ids plus durable-holder contradictions: profiles
    rejected with ``reason='nric_contradiction'`` carry the holder Person id
    when promotion reached the holder check (``None`` for member-only
    contradictions, which have no durable holder yet).
    """
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
    contradictions: list[NricPromotionContradiction] = []
    promoted_members: list[MemberEvidence] = []
    for profile in open_profiles_for_keys(tx, discovery):
        outcome = promote_profile(
            tx,
            profile_id=profile.profile_id,
            identifier=identifier,
            source_key=source_key,
            record_type=record_type,
            incoming=incoming,
            locked_keys=locked_keys,
            prior_promoted=tuple(promoted_members),
        )
        if outcome.status == "promoted":
            promoted.append(profile.profile_id)
            promoted_members.extend(outcome.promoted_members)
        elif outcome.status == "rejected" and outcome.reason == "nric_contradiction":
            contradictions.append(
                NricPromotionContradiction(
                    profile_id=profile.profile_id, holder_person_id=outcome.person_id
                )
            )
    return NricPromotionOutcome(
        promoted_profile_ids=tuple(promoted), contradictions=tuple(contradictions)
    )


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


def _supersede(tx: ManagedTransaction, rows: Sequence[dict[str, str]]) -> list[str]:
    row = tx.run(queries.SUPERSEDE_PROFILE_MEMBERS, rows=list(rows)).single()
    if row is None:
        raise IdentityLinkConflictError("profile supersede returned no row")
    return str_values(row["superseded_pks"])


def _activated_pks(row: Record | None) -> list[str]:
    if row is None:
        raise IdentityLinkConflictError("profile re-attribution returned no row")
    return str_values(row["activated_pks"])


def _reject_with_dispositions(
    tx: ManagedTransaction,
    *,
    profile_id: str,
    expected_version: int,
    supersede_entries: Sequence[dict[str, str]],
    reopen_pks: Sequence[str],
    count_keys: set[tuple[str, str, str]],
) -> PromotionOutcome:
    """Apply version dispositions when no member of this Profile can activate."""
    superseded_pks = _supersede(tx, supersede_entries) if supersede_entries else []
    detached = False
    if superseded_pks:
        row = tx.run(
            queries.DETACH_PROFILE_MEMBERS,
            source_record_pks=superseded_pks,
            profile_id=profile_id,
            expected_version=expected_version,
        ).single()
        profile_ids = [] if row is None else str_values(row["profile_ids"])
        if not profile_ids:
            raise IdentityLinkConflictError(f"profile {profile_id} changed during detach")
        detached = True
    if reopen_pks:
        tx.run(
            queries.REOPEN_PROFILE_MEMBERS,
            profile_id=profile_id,
            source_record_pks=list(reopen_pks),
        )
        detached = True
    if detached:
        recompute_participant_links(tx, "profile", profile_id)
        recompute_participant_counts(tx, count_keys)
    return PromotionOutcome(
        status="rejected", profile_id=profile_id, reason="no_promotable_members"
    )
