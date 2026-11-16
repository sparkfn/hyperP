"""Member-evidence parsing, NRIC rules, and reads for Profile promotion."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph import queries
from src.identity_link_writes import optional_str, required_str
from src.identity_links import IdentityLinkConflictError
from src.identity_registry import FACT_ASSERTION_ATTRIBUTES
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.models import NormalizedAttribute, NormalizedIdentifier, RecordType
from src.nric_anchor import NameDob, nric_anchor_gate
from src.pipeline_writes import attach_person_evidence


@dataclass(frozen=True)
class MemberEvidence:
    source_record_pk: str
    full_name: str | None
    dob: str | None
    nric_entries: tuple[tuple[str, bool | None], ...]

    def name_dob(self) -> NameDob:
        return NameDob(full_name=self.full_name, dob=self.dob)


@dataclass(frozen=True)
class MemberClassification:
    eligible: tuple[MemberEvidence, ...]
    excluded: tuple[str, ...]
    rejection: str | None


@dataclass(frozen=True)
class MemberDisposition:
    source_record_pk: str
    disposition: str
    winner_pk: str | None


def member_evidence(
    source_record_pk: object,
    full_names: object,
    dobs: object,
    nric_entries: object,
) -> MemberEvidence:
    """Parse one ``FETCH_PROFILE_MEMBER_EVIDENCE`` row."""
    return MemberEvidence(
        source_record_pk=required_str_value(source_record_pk),
        full_name=next(iter(str_values(full_names)), None),
        dob=next(iter(str_values(dobs)), None),
        nric_entries=_nric_entries(nric_entries),
    )


def member_has_nric_conflict(member: MemberEvidence, incoming_value: str) -> bool:
    """Whether the member asserts a different digest that is not checksum-invalid."""
    return any(
        value != incoming_value and checksum is not False for value, checksum in member.nric_entries
    )


def member_holder_rejects(
    member: MemberEvidence,
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
) -> bool:
    """Whether the member's own holder facts contradict the incoming record."""
    gate = nric_anchor_gate(identifier, source_key, record_type, incoming, member.name_dob())
    return not gate.promotes_person


def cross_member_contradiction(
    eligible: Sequence[MemberEvidence],
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
) -> bool:
    """Whether any ordered pair of remaining members contradicts itself."""
    for left in eligible:
        for right in eligible:
            if left.source_record_pk == right.source_record_pk:
                continue
            gate = nric_anchor_gate(
                identifier,
                source_key,
                record_type,
                left.name_dob(),
                right.name_dob(),
            )
            if not gate.promotes_person:
                return True
    return False


def _nric_entries(value: object) -> tuple[tuple[str, bool | None], ...]:
    if not isinstance(value, list):
        return ()
    entries: list[tuple[str, bool | None]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        raw_value = item.get("value")
        if not isinstance(raw_value, str):
            continue
        checksum = item.get("checksum_valid")
        entries.append((raw_value, checksum if isinstance(checksum, bool) else None))
    return tuple(entries)


def member_row(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("re-attributed member row is malformed")
    return {str(key): item for key, item in value.items()}


def required_str_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("re-attributed member value is malformed")
    return value


def optional_str_value(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("re-attributed member value is malformed")


def str_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def payload_items(payload: str | None, key: str) -> list[object]:
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


def member_addresses(payload: str | None) -> list[NormalizedAddressModel]:
    addresses: list[NormalizedAddressModel] = []
    for item in payload_items(payload, "addresses"):
        try:
            addresses.append(NormalizedAddressModel.model_validate(item))
        except (TypeError, ValueError):
            continue
    return addresses


def member_attributes(payload: str | None) -> list[NormalizedAttribute]:
    attributes: list[NormalizedAttribute] = []
    for item in payload_items(payload, "attributes"):
        try:
            attribute = NormalizedAttribute.model_validate(item)
        except (TypeError, ValueError):
            continue
        if attribute.attribute_name in FACT_ASSERTION_ATTRIBUTES:
            attributes.append(attribute)
    return attributes


def member_evidence_rows(tx: ManagedTransaction, profile_id: str) -> list[MemberEvidence]:
    """Read every member of a Profile from ``FETCH_PROFILE_MEMBER_EVIDENCE``."""
    return [
        member_evidence(
            row["source_record_pk"],
            row["full_names"],
            row["dobs"],
            row["nric_records"],
        )
        for row in tx.run(queries.FETCH_PROFILE_MEMBER_EVIDENCE, profile_id=profile_id)
    ]


def classify_members(
    members: Sequence[MemberEvidence],
    identifier: NormalizedIdentifier,
    source_key: str,
    record_type: RecordType,
    incoming: NameDob,
    prior_promoted: Sequence[MemberEvidence],
) -> MemberClassification:
    """Split activate-disposition members into eligible/excluded."""
    eligible: list[MemberEvidence] = []
    excluded: list[str] = []
    for member in members:
        if member_has_nric_conflict(member, identifier.normalized_value) or member_holder_rejects(
            member, identifier, source_key, record_type, incoming
        ):
            excluded.append(member.source_record_pk)
        else:
            eligible.append(member)
    if eligible and cross_member_contradiction(
        [*eligible, *prior_promoted], identifier, source_key, record_type
    ):
        return MemberClassification((), (), "nric_contradiction")
    return MemberClassification(tuple(eligible), tuple(excluded), None)


def member_dispositions(
    tx: ManagedTransaction,
    profile_id: str,
    members: Sequence[MemberEvidence],
) -> list[MemberDisposition]:
    """Lock each member's identity and classify its version authority."""
    source_record_pks = [member.source_record_pk for member in members]
    rows = {
        required_str(row, "source_record_pk"): row
        for row in tx.run(
            queries.FETCH_PROFILE_MEMBER_VERSION_DISPOSITION,
            profile_id=profile_id,
            source_record_pks=source_record_pks,
        )
    }
    dispositions: list[MemberDisposition] = []
    for source_record_pk in source_record_pks:
        row = rows.get(source_record_pk)
        if row is None:
            raise IdentityLinkConflictError(f"profile {profile_id} member disposition missing")
        disposition = required_str(row, "disposition")
        if disposition not in ("activate", "supersede", "reopen"):
            raise IdentityLinkConflictError(f"unknown profile member disposition: {disposition}")
        dispositions.append(
            MemberDisposition(
                source_record_pk=source_record_pk,
                disposition=disposition,
                winner_pk=optional_str(row, "winner_pk"),
            )
        )
    return dispositions


def supersede_rows(dispositions: Sequence[MemberDisposition]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in dispositions:
        if item.disposition != "supersede":
            continue
        if item.winner_pk is None:
            raise IdentityLinkConflictError("superseded profile member has no winner version")
        rows.append({"source_record_pk": item.source_record_pk, "winner_pk": item.winner_pk})
    return rows


def attach_member_evidence(
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
        member = member_row(raw_member)
        payload = optional_str_value(member["normalized_payload"])
        attach_person_evidence(
            tx,
            person_id=person_id,
            source_record_pk=required_str_value(member["source_record_pk"]),
            source_system_key=required_str_value(member["source_key"]),
            observed_at=optional_str_value(member["observed_at"]),
            addresses=member_addresses(payload),
            attributes=member_attributes(payload),
            source_trust_tier=None,
            confidence=None,
        )
