"""Deterministic contact/lead row mapper for standalone CRM source facts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC

from src.connectors.bitrix_openlines.crm_identity_policy import (
    crm_standalone_contact_identity_evidence,
    crm_standalone_lead_identity_evidence,
)
from src.connectors.bitrix_openlines.models import CrmContact
from src.models import JsonValue, RawIdentifier, RecordType, SourceRecordEnvelope
from src.standalone_crm_source_fact_models import (
    MalformedSourceFactRow,
    MappedSourceFactRow,
    StandaloneCrmSourceFactMutation,
    StandaloneCrmSourceFactPage,
    strict_row_id,
)


def map_source_fact_page(page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactMutation:
    """Map one authorized page without consulting wall clock or tenant state."""
    mapped: list[MappedSourceFactRow] = []
    malformed: list[MalformedSourceFactRow] = []
    for row in page.rows:
        row_id = strict_row_id(row.id)
        try:
            mapped.append(MappedSourceFactRow(row_id, map_source_fact_row(page, row)))
        except ValueError as exc:
            malformed.append(MalformedSourceFactRow(row_id, str(exc)))
    return StandaloneCrmSourceFactMutation(page, tuple(mapped), tuple(malformed))


def map_source_fact_row(page: StandaloneCrmSourceFactPage, row: CrmContact) -> SourceRecordEnvelope:
    """Create one canonical, source-instance-scoped identity envelope."""
    if row.company_id is not None:
        raise ValueError("company association is not authorized in source-fact rows")
    if row.kind == "contact":
        evidence = crm_standalone_contact_identity_evidence(
            row, source_instance_id=page.envelope.scope.source_instance_id
        )
    elif row.kind == "lead":
        evidence = crm_standalone_lead_identity_evidence(
            row, source_instance_id=page.envelope.scope.source_instance_id
        )
    else:
        raise ValueError("source-fact row kind must be contact or lead")
    upstream_observed_at = _iso(row)
    observed_at = upstream_observed_at or page.envelope.availability.available_at
    kind = row.kind
    source_record_id = f"bitrix-crm-{kind}-{row.id}"
    raw_payload: dict[str, JsonValue] = {
        "source_entity_type": kind,
        "source_entity_id": row.id,
        "source_instance_id": page.envelope.scope.source_instance_id,
        "observed_at": upstream_observed_at,
        "effective_observed_at": observed_at,
        "full_name": row.full_name,
        "identity_metadata": evidence.metadata,
        "standalone_crm_source_fact": {
            "availability_contract_version": page.envelope.availability.contract_version,
            "available_at": page.envelope.availability.available_at,
            "census_id": page.envelope.unit.census_id,
            "stream_kind": kind,
            "generation": page.envelope.unit.generation,
            "fence_token": page.envelope.unit.fence_token,
            "fence_owner_id": page.envelope.unit.fence_owner_id,
            "task_name": page.envelope.unit.task_name,
            "task_id": page.envelope.unit.task_id,
            "payload_digest": page.envelope.unit.payload_digest,
            "frozen_upper_id": page.envelope.frozen_upper_id,
        },
    }
    attributes: dict[str, JsonValue] = {}
    if row.full_name is not None:
        if not row.full_name.strip():
            raise ValueError("full_name cannot be blank when present")
        attributes["full_name"] = row.full_name
    return SourceRecordEnvelope(
        source_system="bitrix_chat",
        source_instance_id=page.envelope.scope.source_instance_id,
        source_record_id=source_record_id,
        record_type=RecordType.IDENTITY,
        ingest_type="standalone_crm_source_fact_v1",
        observed_at=observed_at,
        record_hash=_hash(
            {
                "source_record_id": source_record_id,
                "source_instance_id": page.envelope.scope.source_instance_id,
                "observed_at": upstream_observed_at,
                "identifiers": evidence.identifiers,
                "attributes": attributes,
                "identity_metadata": evidence.metadata,
            }
        ),
        identifiers=[RawIdentifier.model_validate(item) for item in evidence.identifiers],
        attributes=attributes,
        raw_payload=raw_payload,
        source_entity_type=kind,
        source_entity_id=row.id,
        identity_policy_version=str(evidence.metadata["identity_policy_version"]),
        identity_link_key=f"bitrix:{page.envelope.scope.source_instance_id}:{kind}:{row.id}",
    )


def _iso(row: CrmContact) -> str | None:
    if row.observed_at is None:
        return None
    if row.observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return row.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
