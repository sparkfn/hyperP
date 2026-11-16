"""Pure member-evidence parsing and NRIC conflict rules for Profile promotion."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from src.identity_registry import FACT_ASSERTION_ATTRIBUTES
from src.models import (
    NormalizedAddress as NormalizedAddressModel,
)
from src.models import NormalizedAttribute, NormalizedIdentifier, RecordType
from src.nric_anchor import NameDob, nric_anchor_gate


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
