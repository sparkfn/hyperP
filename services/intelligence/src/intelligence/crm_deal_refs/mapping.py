"""Fail-closed allowlist mapping; raw deal JSON is bounded and never persisted."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from intelligence.crm_deal_refs.models import (
    IDENTITY_POLICY_VERSION,
    MAX_PAYLOAD_BYTES,
    SOURCE_SYSTEM,
    DealKey,
    DealReference,
    IdentityRevision,
    IdentityStatus,
    utc_now,
)
from intelligence.repositories.protocols.crm_deal_refs import DealReferenceRow, IdentityRevisionRow

_IDENTITY_STATUSES = frozenset(
    {"resolved", "unresolved", "pending_review", "blocked", "rejected", "retired"}
)
_RESOLUTION_KINDS = frozenset(
    {
        "baseline",
        "automatic_activation",
        "reviewed_activation",
        "review_rejection",
        "manual_no_match",
        "source_supersession",
        "person_merge",
        "person_unmerge",
        "person_retirement",
        "source_retirement",
    }
)


def map_deal_reference(row: DealReferenceRow, source_instance_id: str, as_of: str) -> DealReference:
    key, record_hash, policy, entity_id = _deal_provenance(row)
    payload = _payload(row["raw_payload"])
    source_payload = _source_deal_payload(payload)
    _payload_identity(payload, source_payload, entity_id)
    _payload_policy(payload, policy)
    stage = _deal_text(payload, source_payload, "stage_id", "STAGE_ID")
    projection = _optional_text(row["stage_id"], "stage_id")
    if stage is not None and projection is not None and stage != projection:
        raise ValueError("crm deal stage projection conflicts with payload")
    cutoff = _cutoff(as_of)
    event_at = _deal_timestamp(payload, source_payload, "DATE_CREATE")
    effective_at = _deal_timestamp(payload, source_payload, "DATE_MODIFY")
    close_at = _deal_timestamp(payload, source_payload, "CLOSEDATE")
    observed_at = _timestamp(row["observed_at"], "observed_at", required=False)
    available_at = _timestamp(row["ingested_at"], "ingested_at", required=False)
    category_id = _deal_text(payload, source_payload, "category_id", "CATEGORY_ID")
    stage_semantic_id = _deal_text(
        payload, source_payload, "stage_semantic_id", "STAGE_SEMANTIC_ID"
    )
    eligible = (
        available_at is not None
        and available_at <= cutoff
        and observed_at is not None
        and observed_at <= cutoff
        and event_at is not None
        and event_at <= cutoff
        and effective_at is not None
        and effective_at <= cutoff
        and (close_at is None or close_at <= cutoff)
        and category_id is not None
        and (stage is not None or stage_semantic_id is not None)
    )
    return DealReference(
        source_system=SOURCE_SYSTEM,
        source_instance_id=source_instance_id,
        identity_policy_version=policy,
        key=key,
        record_hash=record_hash,
        source_entity_type="deal",
        source_entity_id=entity_id,
        entity_key=_deal_entity_key(row),
        category_id=category_id,
        stage_id=projection if projection is not None else stage,
        stage_semantic_id=stage_semantic_id,
        source_outcome_ref=stage_semantic_id,
        observed_at=observed_at,
        available_at=available_at,
        first_known_at=available_at,
        source_event_at=event_at,
        source_effective_at=effective_at,
        source_close_date=close_at,
        availability=(
            "known_by_cutoff" if available_at is not None and available_at <= cutoff else "unknown"
        ),
        point_in_time_eligible=eligible,
        lifecycle_status_observed=_optional_text(row["lifecycle_status"], "lifecycle_status"),
        link_status_observed=_optional_text(row["link_status"], "link_status"),
        observation_captured_at=utc_now(),
    )


def _deal_provenance(row: DealReferenceRow) -> tuple[DealKey, str, str, str]:
    entity_type = _text(row["source_entity_type"], "source_entity_type")
    if entity_type != "deal":
        raise ValueError("crm deal source entity type is invalid")
    key = DealKey(
        _text(row["source_record_id"], "source_record_id"),
        _positive(row["source_record_version"], "source_record_version"),
        _text(row["source_record_pk"], "source_record_pk"),
    )
    return (
        key,
        _hash(row["record_hash"]),
        _policy(row["identity_policy_version"]),
        _text(row["source_entity_id"], "source_entity_id"),
    )


def _deal_entity_key(row: DealReferenceRow) -> str | None:
    owned = row["owned_entity_keys"]
    if not isinstance(owned, list) or any(
        item is not None and not isinstance(item, str) for item in owned
    ):
        raise ValueError("owned entity evidence is invalid")
    entity_keys = tuple(sorted({item for item in owned if isinstance(item, str)}))
    owned_count = row["owned_entity_count"]
    if not isinstance(owned_count, int) or isinstance(owned_count, bool) or owned_count < 0:
        raise ValueError("owned entity count is invalid")
    if owned_count > 1 or (owned_count == 1 and len(entity_keys) != 1):
        raise ValueError("crm deal ownership is ambiguous")
    record_key = _optional_text(row["record_entity_key"], "record_entity_key")
    if entity_keys and record_key is not None and entity_keys[0] != record_key:
        raise ValueError("crm deal ownership conflicts with record entity")
    return entity_keys[0] if entity_keys else record_key


def map_identity_revision(
    row: IdentityRevisionRow, source_instance_id: str, as_of: str
) -> IdentityRevision:
    instance = _text(row["source_instance_id"], "source_instance_id")
    if instance != source_instance_id:
        raise ValueError("identity source instance conflicts with boundary")
    policy = _policy(row["identity_policy_version"])
    status = _text(row["link_status"], "link_status")
    if status not in _IDENTITY_STATUSES:
        raise ValueError("identity link status is invalid")
    person = _optional_text(row["hyperp_person_id"], "hyperp_person_id")
    person_status = _optional_text(row["person_status"], "person_status")
    available_at = _timestamp(row["created_at"], "created_at", required=False)
    effective_at = _timestamp(row["effective_at"], "effective_at", required=False)
    known = available_at is not None and available_at <= _cutoff(as_of)
    effective_known = effective_at is not None and effective_at <= _cutoff(as_of)
    person_visible = status == "resolved" and known and effective_known
    if status == "resolved" and person_visible:
        if person is None or person_status is None:
            raise ValueError("resolved identity requires a Person UUID and observed status")
        try:
            UUID(person)
        except ValueError as error:
            raise ValueError("resolved identity Person UUID is invalid") from error
    elif status == "resolved":
        person = None
        person_status = None
    elif person is not None or person_status is not None:
        raise ValueError("non-resolved identity must not expose Person data")
    resolution_kind = _text(row["resolution_kind"], "resolution_kind")
    if resolution_kind not in _RESOLUTION_KINDS:
        raise ValueError("identity resolution kind is incompatible")
    return IdentityRevision(
        event_id=_text(row["event_id"], "event_id"),
        global_revision=_positive(row["global_revision"], "global_revision"),
        source_system=SOURCE_SYSTEM,
        source_instance_id=instance,
        identity_policy_version=policy,
        source_entity_id=_text(row["source_entity_id"], "source_entity_id"),
        link_status=cast(IdentityStatus, status),
        hyperp_person_id=person,
        person_status_observed=person_status,
        person_observation_captured_at=utc_now() if person_status is not None else None,
        resolution_kind=resolution_kind,
        resolution_revision=_positive(row["resolution_revision"], "resolution_revision"),
        effective_at=effective_at,
        available_at=available_at,
        first_known_at=available_at,
        availability="known_by_cutoff" if known else "unknown",
        person_reference_eligible=person_visible,
    )


def _payload(value: object) -> dict[str, object]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError("crm deal raw payload is invalid")
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("crm deal raw payload is invalid") from error
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise ValueError("crm deal raw payload is invalid")
    return {key: item for key, item in raw.items()}


def _source_deal_payload(payload: dict[str, object]) -> dict[str, object]:
    value = payload.get("deal")
    if value is None:
        return {}
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError("crm deal source payload is invalid")
    return {str(key): item for key, item in value.items()}


def _payload_identity(
    payload: dict[str, object], source_payload: dict[str, object], entity_id: str
) -> None:
    values = (
        _payload_text(payload, "crm_deal_id"),
        _payload_text(payload, "ID"),
        _payload_text(source_payload, "ID"),
    )
    present = {value for value in values if value is not None}
    if len(present) > 1 or (present and present != {entity_id}):
        raise ValueError("crm deal payload identity conflicts with source identity")


def _payload_policy(payload: dict[str, object], policy: str) -> None:
    embedded = _payload_text(payload, "crm_deal_identity_policy_version")
    if embedded is not None and embedded != policy:
        raise ValueError("crm deal payload identity policy conflicts with source record")


def _deal_text(
    payload: dict[str, object],
    source_payload: dict[str, object],
    outer_key: str,
    source_key: str,
) -> str | None:
    values = (
        _payload_text(payload, outer_key),
        _payload_text(payload, source_key),
        _payload_text(source_payload, source_key),
    )
    present = {value for value in values if value is not None}
    if len(present) > 1:
        raise ValueError(f"crm deal payload {source_key} values conflict")
    return next(iter(present), None)


def _deal_timestamp(
    payload: dict[str, object], source_payload: dict[str, object], key: str
) -> str | None:
    value = _deal_text(payload, source_payload, key.lower(), key)
    return None if value is None else _timestamp(value, key, required=False)


def _payload_text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _text(value, key)


def _timestamp(value: object, field: str, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} is not ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _cutoff(value: str) -> str:
    return _timestamp(value, "as_of", required=True) or ""


def _policy(value: object) -> str:
    policy = _text(value, "identity_policy_version")
    if policy != IDENTITY_POLICY_VERSION:
        raise ValueError("identity policy is incompatible")
    return policy


def _hash(value: object) -> str:
    digest = _text(value, "record_hash")
    raw = digest.removeprefix("sha256:")
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw):
        raise ValueError("record_hash is invalid")
    return digest


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is invalid")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _positive(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} is invalid")
    return value
