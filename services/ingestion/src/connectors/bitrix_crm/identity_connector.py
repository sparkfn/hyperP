"""Standalone Bitrix CRM contact, lead, and company source-record connector."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Protocol

from src.connectors.base import SourceConnector
from src.connectors.bitrix_openlines.crm_identity_policy import (
    crm_company_reference_evidence,
    crm_standalone_contact_identity_evidence,
    crm_standalone_lead_identity_evidence,
)
from src.connectors.bitrix_openlines.models import CrmCompany, CrmContact
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue
from src.source_instances import canonical_source_instance_id


class CrmIdentityClient(Protocol):
    """Read-only paginated traversal contract for standalone CRM identity."""

    def iter_crm_contacts(self) -> Iterator[CrmContact]: ...

    def iter_crm_leads(self) -> Iterator[CrmContact]: ...

    def iter_crm_companies(self) -> Iterator[CrmCompany]: ...

    def close(self) -> None: ...


class BitrixCrmIdentityConnector(SourceConnector):
    """Emit standalone portal-scoped CRM records without using CRM deals."""

    def __init__(self, client: CrmIdentityClient, config: BitrixOpenLinesConfig) -> None:
        if config.source_instance_id is None:
            raise ValueError("standalone Bitrix CRM identity requires source_instance_id")
        self._client = client
        self._source_instance_id = canonical_source_instance_id(config.source_instance_id)

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        for contact in self._client.iter_crm_contacts():
            yield _person_envelope(
                contact,
                source_instance_id=self._source_instance_id,
                entity_type="contact",
            )
        for lead in self._client.iter_crm_leads():
            yield _person_envelope(
                lead,
                source_instance_id=self._source_instance_id,
                entity_type="lead",
            )
        for company in self._client.iter_crm_companies():
            yield _company_envelope(company, source_instance_id=self._source_instance_id)

    def close(self) -> None:
        self._client.close()


def _person_envelope(
    record: CrmContact,
    *,
    source_instance_id: str,
    entity_type: str,
) -> dict[str, JsonValue]:
    if entity_type == "contact":
        evidence = crm_standalone_contact_identity_evidence(
            record,
            source_instance_id=source_instance_id,
        )
    elif entity_type == "lead":
        evidence = crm_standalone_lead_identity_evidence(
            record,
            source_instance_id=source_instance_id,
        )
    else:
        raise ValueError("standalone CRM entity_type must be contact or lead")
    raw_payload: dict[str, JsonValue] = {
        "source_entity_type": entity_type,
        "source_entity_id": record.id,
        "source_instance_id": source_instance_id,
        "observed_at": _iso_or_none(record.observed_at),
        "full_name": record.full_name,
        "identity_policy_version": evidence.metadata["identity_policy_version"],
        "raw_identifier_group": _raw_identifier_group(record, entity_type),
        "identity_metadata": evidence.metadata,
    }
    attributes: dict[str, JsonValue] = {}
    if record.full_name is not None:
        attributes["full_name"] = record.full_name
    return {
        "source_record_id": f"bitrix-crm-{entity_type}-{record.id}",
        "source_instance_id": source_instance_id,
        "record_type": "identity",
        "ingest_type": "api_incremental",
        "observed_at": _iso_or_none(record.observed_at),
        "record_hash": _hash_payload(raw_payload),
        "identifiers": list(evidence.identifiers),
        "attributes": attributes,
        "raw_payload": raw_payload,
    }


def _company_envelope(company: CrmCompany, *, source_instance_id: str) -> dict[str, JsonValue]:
    evidence = crm_company_reference_evidence(company, source_instance_id=source_instance_id)
    raw_payload: dict[str, JsonValue] = {
        "source_entity_type": "company",
        "source_entity_id": company.id,
        "source_instance_id": source_instance_id,
        "observed_at": _iso_or_none(company.observed_at),
        "company_title": company.title,
        "company_reference": evidence.reference,
        "reference_metadata": evidence.metadata,
    }
    attributes: dict[str, JsonValue] = {}
    if company.title is not None:
        attributes["company_title"] = company.title
    return {
        "source_record_id": f"bitrix-crm-company-{company.id}",
        "source_instance_id": source_instance_id,
        "record_type": "crm_company",
        "ingest_type": "api_incremental",
        "observed_at": _iso_or_none(company.observed_at),
        "record_hash": _hash_payload(raw_payload),
        "identifiers": [],
        "attributes": attributes,
        "raw_payload": raw_payload,
    }


def _raw_identifier_group(record: CrmContact, entity_type: str) -> list[JsonValue]:
    identifier_type = "crm_contact_id" if entity_type == "contact" else "crm_lead_id"
    identifiers: list[JsonValue] = [
        {"type": identifier_type, "value": record.id, "is_verified": True}
    ]
    identifiers.extend(
        {"type": "phone", "value": value, "is_verified": False} for value in record.phones
    )
    identifiers.extend(
        {"type": "email", "value": value, "is_verified": False} for value in record.emails
    )
    return identifiers


def _hash_payload(payload: dict[str, JsonValue]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
