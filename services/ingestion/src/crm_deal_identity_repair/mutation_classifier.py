"""Strict inventory parsing and provenance-aware CRM-deal repair planning."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from src.connectors.bitrix_openlines.connector import build_crm_deal_envelope
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import (
    RepairAuthorityEvidence,
    RepairMutationPlan,
)
from src.models import JsonValue, MatchResult, SourceRecordEnvelope
from src.pipeline_crm_identity import CrmOwnerProvenance, plan_crm_deal_identity
from src.pipeline_normalization import normalize_envelope_identifiers

_INVENTORY_KEYS = frozenset(
    {
        "source_record_version",
        "lifecycle_status",
        "is_latest",
        "record_hash",
        "observed_at",
        "raw_payload",
        "normalized_payload",
        "linked_people",
        "projections",
        "logical_version_evidence",
        "lifecycle_policy_evidence",
        "descendants",
        "decisions_and_reviews",
        "owner_impacts",
    }
)


@dataclass(frozen=True)
class ParsedRepairInventory:
    """Validated frozen row plus an exact rebuilt v2 source envelope when possible."""

    item: RepairInventoryItem
    envelope: SourceRecordEnvelope | None
    source_record_version: int
    current_owner_ids: tuple[str, ...]
    descendant_source_record_pks: tuple[str, ...]
    reconstructable: bool


def parse_repair_inventory(
    item: RepairInventoryItem,
    source_instance_id: str,
    entity_key: str,
) -> ParsedRepairInventory:
    """Decode one qualified inventory row without accepting ambient or fabricated data."""
    if item.partition == "negative_control":
        raise ValueError("negative-control inventory is not a repair mutation input")
    if not source_instance_id or not entity_key:
        raise ValueError("repair reconstruction requires locked source and entity identities")
    payload = item.payload
    if set(payload) != _INVENTORY_KEYS:
        raise ValueError("repair inventory payload schema differs from the qualified contract")
    if not _is_sha256_digest(_required_string(payload, "record_hash")):
        raise ValueError("repair inventory record hash is invalid")
    version = _positive_version(payload["source_record_version"])
    raw_payload = _decoded_json_object(payload["raw_payload"], "raw_payload")
    normalized_payload = _decoded_json_object(payload["normalized_payload"], "normalized_payload")
    owners = _active_owner_ids(payload["linked_people"])
    descendants = _descendant_source_record_pks(payload["descendants"], item.source_record_pk)
    envelope = _rebuild_v2_envelope(
        item,
        raw_payload,
        normalized_payload,
        payload["observed_at"],
        source_instance_id,
        entity_key,
    )
    if envelope is not None:
        envelope = envelope.model_copy(update={"source_record_version": str(version + 1)})
    return ParsedRepairInventory(
        item=item,
        envelope=envelope,
        source_record_version=version + 1,
        current_owner_ids=owners,
        descendant_source_record_pks=descendants,
        reconstructable=envelope is not None,
    )


def build_repair_plan(
    parsed: ParsedRepairInventory,
    match_result: MatchResult,
    authority_evidence: tuple[RepairAuthorityEvidence, ...],
) -> RepairMutationPlan:
    """Build a desired state that never grants authority to unreconstructable input."""
    source_record_pk = str(
        uuid5(
            NAMESPACE_URL,
            parsed.item.source_record_pk
            + ":crm_deal_identity_v2:"
            + str(parsed.source_record_version),
        )
    )
    if parsed.envelope is None:
        provisional = parsed.current_owner_ids[0] if len(parsed.current_owner_ids) == 1 else None
        return RepairMutationPlan(
            disposition="review_required",
            source_record_payload=None,
            source_record_pk=source_record_pk,
            source_record_version=parsed.source_record_version,
            selected_person_id=None,
            provisional_person_id=provisional,
            current_owner_ids=parsed.current_owner_ids,
            authority_evidence=authority_evidence,
            reason_codes=("unreconstructable_v2_payload",),
            retired_source_record_pks=(
                parsed.item.source_record_pk,
                *parsed.descendant_source_record_pks,
            ),
        )
    identifiers = normalize_envelope_identifiers(parsed.envelope)
    provenance = tuple(
        CrmOwnerProvenance(
            person_id=evidence.person_id,
            provenance_class=evidence.provenance_class,
            supporting_source_record_pks=evidence.source_record_pks,
        )
        for evidence in authority_evidence
    )
    policy = plan_crm_deal_identity(
        parsed.envelope,
        identifiers,
        match_result,
        current_owner_ids=parsed.current_owner_ids,
        owner_provenance=provenance,
        repair_source_record_pk=parsed.item.source_record_pk,
    )
    disposition: Literal["applied", "review_required"] = (
        "applied" if policy.selected_person_id is not None else "review_required"
    )
    reasons = tuple(sorted(set(policy.reason_codes))) or ("repair_requires_review",)
    return RepairMutationPlan(
        disposition=disposition,
        source_record_payload=_json_payload(parsed.envelope.model_dump(mode="json")),
        source_record_pk=source_record_pk,
        source_record_version=parsed.source_record_version,
        selected_person_id=policy.selected_person_id if disposition == "applied" else None,
        provisional_person_id=(
            policy.provisional_person_id if disposition == "review_required" else None
        ),
        current_owner_ids=parsed.current_owner_ids,
        authority_evidence=authority_evidence,
        reason_codes=reasons,
        retired_source_record_pks=(
            parsed.item.source_record_pk,
            *parsed.descendant_source_record_pks,
        ),
    )


def _rebuild_v2_envelope(
    item: RepairInventoryItem,
    raw_payload: Mapping[str, JsonValue],
    normalized_payload: Mapping[str, JsonValue],
    observed_at: object,
    source_instance_id: str,
    entity_key: str,
) -> SourceRecordEnvelope | None:
    observed = _aware_datetime(observed_at)
    deal_id = _optional_string(raw_payload.get("crm_deal_id"))
    title = _optional_string(raw_payload.get("title"))
    contact_rows = raw_payload.get("crm_contact_raw_groups")
    raw_deal_value = raw_payload.get("deal")
    if (
        observed is None
        or deal_id != item.deal_id
        or title is None
        or not isinstance(contact_rows, list)
        or not isinstance(raw_deal_value, dict)
    ):
        return None
    contacts = _contacts_from_raw_groups(contact_rows, normalized_payload)
    primary_contact_id = _optional_string(raw_payload.get("primary_contact_id"))
    primary = next((contact for contact in contacts if contact.id == primary_contact_id), None)
    rebuilt = build_crm_deal_envelope(
        CrmDeal(
            id=deal_id,
            title=title,
            category_id=_optional_string(raw_payload.get("category_id")),
            stage_id=_optional_string(raw_payload.get("stage_id")),
            observed_at=observed,
            primary_contact=primary,
            contacts=contacts,
            contact_count=_nonnegative_int(raw_payload.get("contact_count"), len(contacts)),
            has_ambiguous_contacts=raw_payload.get("crm_contact_resolution_required") is True,
            raw_payload=_json_object(raw_deal_value),
        ),
        entity_key,
        source_instance_id=source_instance_id,
    )
    envelope = SourceRecordEnvelope.model_validate(
        {"source_system": "bitrix_chat", "source_instance_id": source_instance_id, **rebuilt}
    )
    expected_observed = observed.isoformat()
    if (
        envelope.source_record_id != item.source_record_id
        or envelope.entity_key != entity_key
        or envelope.source_instance_id != source_instance_id
        or envelope.observed_at != expected_observed
        or envelope.identity_policy_version != "crm_deal_identity_v2"
        or envelope.identity_link_key != f"bitrix:{source_instance_id}:deal:{item.deal_id}"
        or envelope.record_hash != _required_string(item.payload, "record_hash")
        or not _is_sha256_digest(envelope.record_hash)
        or any(
            identifier.type == "crm_contact_id"
            and identifier.source_instance_id != source_instance_id
            for identifier in envelope.identifiers
        )
    ):
        return None
    if not _normalized_payload_matches(envelope, normalized_payload):
        return None
    return envelope


def _contacts_from_raw_groups(
    value: list[JsonValue],
    normalized_payload: Mapping[str, JsonValue],
) -> tuple[CrmContact, ...]:
    full_name = _frozen_full_name(normalized_payload)
    contacts: list[CrmContact] = []
    for group in value:
        if not isinstance(group, list):
            return ()
        values: dict[str, list[str]] = {"crm_contact_id": [], "phone": [], "email": []}
        for item in group:
            if not isinstance(item, dict):
                return ()
            kind = item.get("type")
            item_value = item.get("value")
            if (
                isinstance(kind, str)
                and kind in values
                and isinstance(item_value, str)
                and item_value
            ):
                values[kind].append(item_value)
        if not values["crm_contact_id"]:
            return ()
        contacts.append(
            CrmContact(
                id=values["crm_contact_id"][0],
                full_name=full_name if len(contacts) == 0 else None,
                phones=tuple(sorted(set(values["phone"]))),
                emails=tuple(sorted(set(values["email"]))),
            )
        )
    return tuple(contacts)


def _frozen_full_name(normalized_payload: Mapping[str, JsonValue]) -> str | None:
    """Return a primary-contact name only when frozen normalized evidence retained it."""
    attributes = normalized_payload.get("attributes")
    if not isinstance(attributes, dict):
        return None
    value = attributes.get("full_name")
    return value if isinstance(value, str) and value else None


def _normalized_payload_matches(
    envelope: SourceRecordEnvelope,
    frozen: Mapping[str, JsonValue],
) -> bool:
    """Fail closed when a deterministic rebuild would lose normalized v2 evidence."""
    if not frozen:
        return True
    attributes = frozen.get("attributes")
    if attributes is not None and attributes != envelope.attributes:
        return False
    identifiers = frozen.get("identifiers")
    if identifiers is not None:
        rebuilt = [item.model_dump(mode="json") for item in envelope.identifiers]
        if identifiers != rebuilt:
            return False
    return True


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_sha256_digest(value: str) -> bool:
    raw = value.removeprefix("sha256:")
    return len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)


def _decoded_json_object(value: object, label: str) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return _json_object(value)
    if not isinstance(value, str):
        raise ValueError(f"repair inventory {label} must be a JSON object")
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"repair inventory {label} is malformed") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"repair inventory {label} must be a JSON object")
    return _json_object(decoded)


def _json_payload(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise RuntimeError("repair source envelope serialization is not an object")
    return _json_object(value)


def _json_object(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("repair inventory JSON object is invalid")
    return {key: _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return _json_object(value)
    raise ValueError("repair inventory contains a non-JSON value")


def _active_owner_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("repair inventory linked_people must be a list")
    owners = {
        person_id
        for link in value
        if isinstance(link, dict)
        and link.get("is_active") is not False
        and isinstance(person_id := link.get("person_id"), str)
        and person_id
    }
    return tuple(sorted(owners))


def _descendant_source_record_pks(value: object, current_pk: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("repair inventory descendants must be a list")
    result: set[str] = set()
    for row in value:
        if not isinstance(row, dict):
            raise ValueError("repair inventory descendants must contain objects")
        pk = row.get("source_record_pk")
        if isinstance(pk, str) and pk and pk != current_pk:
            result.add(pk)
    return tuple(sorted(result))


def _positive_version(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal() and not value.startswith("0"):
        return int(value)
    raise ValueError("repair inventory source_record_version must be a positive integer")


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"repair inventory {key} must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object, fallback: int) -> int:
    return (
        value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else fallback
    )
