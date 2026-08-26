"""Bounded standalone Bitrix CRM identity source-record connectors."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Literal, Protocol

from src.connectors.base import SourceConnector
from src.connectors.bitrix_openlines.crm_identity_policy import (
    crm_company_reference_evidence,
    crm_standalone_contact_identity_evidence,
    crm_standalone_lead_identity_evidence,
)
from src.connectors.bitrix_openlines.models import (
    CrmCompany,
    CrmCompanyBindingPayload,
    CrmContact,
    CrmIdentityKeysetPage,
)
from src.crm_identity_associations import (
    CrmCompanyMembershipSnapshot,
    lead_membership_snapshot,
    normalize_company_membership_snapshot,
)
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue
from src.source_instances import canonical_source_instance_id

CrmIdentityKind = Literal["contact", "lead", "company"]


class CrmIdentityClient(Protocol):
    """Read-only bounded traversal contract for standalone CRM identity."""

    def list_crm_contacts_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage: ...

    def list_crm_leads_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage: ...

    def list_crm_companies_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage: ...

    def get_contact_company_bindings(
        self, contact_id: str
    ) -> tuple[CrmCompanyBindingPayload, ...]: ...

    @property
    def request_count(self) -> int: ...

    def close(self) -> None: ...


class _BitrixCrmIdentityKeysetConnector(SourceConnector):
    """One bounded child reader intended for a future fenced census dispatcher."""

    def __init__(
        self,
        client: CrmIdentityClient,
        config: BitrixOpenLinesConfig,
        *,
        kind: CrmIdentityKind,
        upper_id: int,
        last_id: int | None = None,
    ) -> None:
        if config.source_instance_id is None:
            raise ValueError("standalone Bitrix CRM identity requires source_instance_id")
        if kind not in {"contact", "lead", "company"}:
            raise ValueError("standalone CRM identity kind is invalid")
        if isinstance(upper_id, bool) or upper_id < 0:
            raise ValueError("standalone CRM identity upper_id must be non-negative")
        if last_id is not None and (isinstance(last_id, bool) or last_id < 1 or last_id > upper_id):
            raise ValueError("standalone CRM identity last_id must be within the frozen window")
        self._client = client
        self._source_instance_id = canonical_source_instance_id(config.source_instance_id)
        self._association_contract_version = config.crm_identity_association_contract_version
        self._kind = kind
        self._upper_id = upper_id
        self._last_id = last_id

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        if self._upper_id == 0 or self._last_id == self._upper_id:
            return
        cursor = self._last_id
        first_page = cursor is None
        while True:
            page = self._keyset_page(cursor)
            if page.upper_id != self._upper_id:
                raise RuntimeError("Bitrix standalone CRM keyset changed its frozen upper bound")
            records = page.records
            if len(records) > 50:
                raise RuntimeError("Bitrix standalone CRM keyset exceeded the fixed page size")
            ids = [int(record.id) for record in records]
            if first_page and self._upper_id > 0 and not records:
                raise RuntimeError(
                    "Bitrix standalone CRM keyset returned an empty first page "
                    "for a positive window"
                )
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise RuntimeError("Bitrix standalone CRM keyset was not strictly increasing")
            if cursor is not None and ids and ids[0] <= cursor:
                raise RuntimeError("Bitrix standalone CRM keyset did not advance")
            for record in records:
                yield self._envelope(record)
            if len(records) < 50:
                return
            if not ids:
                raise RuntimeError("Bitrix standalone CRM keyset returned an invalid full page")
            cursor = ids[-1]
            first_page = False
            if cursor == self._upper_id:
                return

    def _keyset_page(self, cursor: int | None) -> CrmIdentityKeysetPage:
        if self._kind == "contact":
            return self._client.list_crm_contacts_keyset(
                greater_than_id=cursor,
                less_than_or_equal_to_id=self._upper_id,
            )
        if self._kind == "lead":
            return self._client.list_crm_leads_keyset(
                greater_than_id=cursor,
                less_than_or_equal_to_id=self._upper_id,
            )
        return self._client.list_crm_companies_keyset(
            greater_than_id=cursor,
            less_than_or_equal_to_id=self._upper_id,
        )

    def _envelope(self, record: CrmContact | CrmCompany) -> dict[str, JsonValue]:
        if self._kind == "company":
            if not isinstance(record, CrmCompany):
                raise RuntimeError("Bitrix company keyset returned an identity record")
            return _company_envelope(record, source_instance_id=self._source_instance_id)
        if not isinstance(record, CrmContact):
            raise RuntimeError("Bitrix identity keyset returned a company record")
        snapshot = self._snapshot(record)
        return _person_envelope(
            record,
            source_instance_id=self._source_instance_id,
            entity_type=self._kind,
            membership_snapshot=snapshot,
        )

    def _snapshot(self, record: CrmContact) -> CrmCompanyMembershipSnapshot:
        if self._kind == "contact":
            return normalize_company_membership_snapshot(
                subject_type="contact",
                subject_id=record.id,
                payloads=self._client.get_contact_company_bindings(record.id),
                contract_version=self._association_contract_version,
            )
        return lead_membership_snapshot(
            lead_id=record.id,
            company_id=record.company_id,
            contract_version=self._association_contract_version,
        )

    @property
    def request_count(self) -> int:
        """Expose all source HTTP attempts, including contact binding reads."""
        return self._client.request_count

    def close(self) -> None:
        self._client.close()


def _person_envelope(
    record: CrmContact,
    *,
    source_instance_id: str,
    entity_type: str,
    membership_snapshot: CrmCompanyMembershipSnapshot | None = None,
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
    if membership_snapshot is not None:
        raw_payload["crm_company_membership"] = {
            "contract_version": membership_snapshot.contract_version,
            "digest": membership_snapshot.digest,
            "bindings": [
                {
                    "company_id": binding.company_id,
                    "sort": binding.sort,
                    "role_id": binding.role_id,
                    "is_primary": binding.is_primary,
                }
                for binding in membership_snapshot.bindings
            ],
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
