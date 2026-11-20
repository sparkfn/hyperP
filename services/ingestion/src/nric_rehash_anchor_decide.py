"""Pure policy decisions for safely anchoring existing Persons by NRIC."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from src.identity_link_member_evidence import (
    MemberEvidence,
    cross_member_contradiction,
    member_attributes,
    member_evidence,
)
from src.models import NormalizedIdentifier, QualityFlag, RecordType
from src.normalizers.nric import is_nric_digest
from src.nric_anchor import NameDob, NricGateReason, nric_anchor_gate

AnchorKind = Literal["anchor", "already_anchored", "handoff"]
AnchorReason = (
    Literal[
        "already_anchored",
        "multi_nric",
        "unrecoverable_identifier",
        "person_key_conflict",
        "checksum_invalid",
        "shared_owner",
        "evidence_truncated",
        "missing_source_context",
        "nric_contradiction",
    ]
    | NricGateReason
)


@dataclass(frozen=True)
class AnchorIdentifier:
    identifier_id: str
    normalized_value: str | None
    checksum_valid: bool | None
    other_key_holders: int
    other_active_evidence: int


@dataclass(frozen=True)
class AnchorSourceRecord:
    source_record_pk: str
    source_key: str | None
    record_type: str | None
    normalized_payload: str | None


@dataclass(frozen=True)
class AnchorEvidence:
    person_id: str
    nric_key: str | None
    has_anchor: bool
    identifiers: tuple[AnchorIdentifier, ...]
    records: tuple[AnchorSourceRecord, ...]
    evidence_truncated: bool


@dataclass(frozen=True)
class AnchorDecision:
    kind: AnchorKind
    reason: AnchorReason


@dataclass(frozen=True)
class _RecordGateOutcome:
    eligible: tuple[MemberEvidence, ...]
    first_rejection: NricGateReason | None
    representative: tuple[str, RecordType] | None
    usable_record_found: bool


def decide_anchor(
    evidence: AnchorEvidence,
    key_version: str,
    durable_holders: Sequence[MemberEvidence],
) -> AnchorDecision:
    """Choose anchor, already-anchored, or a sanitized L2 handoff reason."""
    if len(evidence.identifiers) > 1:
        return _handoff("multi_nric")
    if not evidence.identifiers:
        return _handoff("unrecoverable_identifier")
    identifier_evidence = evidence.identifiers[0]
    digest = identifier_evidence.normalized_value
    if digest is None or not is_nric_digest(digest):
        return _handoff("unrecoverable_identifier")
    if evidence.nric_key == digest and evidence.has_anchor:
        return AnchorDecision("already_anchored", "already_anchored")
    if evidence.nric_key is not None and evidence.nric_key != digest:
        return _handoff("person_key_conflict")
    if identifier_evidence.checksum_valid is not True:
        return _handoff("checksum_invalid")
    if identifier_evidence.other_key_holders or identifier_evidence.other_active_evidence:
        return _handoff("shared_owner")
    if evidence.evidence_truncated:
        return _handoff("evidence_truncated")

    identifier = NormalizedIdentifier(
        identifier_type="nric",
        normalized_value=digest,
        quality_flag=QualityFlag.VALID,
        checksum_valid=True,
        nric_key_version=key_version,
    )
    outcome = _gate_source_records(evidence.records, identifier)
    if not outcome.usable_record_found:
        return _handoff("missing_source_context")
    if not outcome.eligible or outcome.representative is None:
        return _handoff(outcome.first_rejection or "missing_source_context")
    source_key, record_type = outcome.representative
    if cross_member_contradiction(
        outcome.eligible,
        identifier,
        source_key,
        record_type,
        durable_holders=durable_holders,
    ):
        return _handoff("nric_contradiction")
    return AnchorDecision("anchor", "anchor")


def _gate_source_records(
    records: Sequence[AnchorSourceRecord],
    identifier: NormalizedIdentifier,
) -> _RecordGateOutcome:
    eligible: list[MemberEvidence] = []
    first_rejection: NricGateReason | None = None
    representative: tuple[str, RecordType] | None = None
    usable_record_found = False
    for record in sorted(records, key=lambda item: item.source_record_pk):
        context = _record_context(record)
        if context is None:
            continue
        source_key, record_type, incoming = context
        usable_record_found = True
        gate = nric_anchor_gate(identifier, source_key, record_type, incoming, None)
        if not gate.promotes_person:
            if first_rejection is None:
                first_rejection = gate.reason
            continue
        eligible.append(
            member_evidence(
                record.source_record_pk,
                [incoming.full_name],
                [incoming.dob],
                [],
            )
        )
        if representative is None:
            representative = (source_key, record_type)
    return _RecordGateOutcome(
        eligible=tuple(eligible),
        first_rejection=first_rejection,
        representative=representative,
        usable_record_found=usable_record_found,
    )


def _record_context(
    record: AnchorSourceRecord,
) -> tuple[str, RecordType, NameDob] | None:
    if not record.source_key:
        return None
    if not isinstance(record.record_type, str):
        return None
    try:
        record_type = RecordType(record.record_type)
    except ValueError:
        return None
    attributes = member_attributes(record.normalized_payload)
    full_name = next(
        (
            attribute.attribute_value
            for attribute in attributes
            if attribute.attribute_name == "full_name"
        ),
        None,
    )
    dob = next(
        (
            attribute.attribute_value
            for attribute in attributes
            if attribute.attribute_name == "dob"
        ),
        None,
    )
    return record.source_key, record_type, NameDob(full_name=full_name, dob=dob)


def _handoff(reason: AnchorReason) -> AnchorDecision:
    return AnchorDecision("handoff", reason)
