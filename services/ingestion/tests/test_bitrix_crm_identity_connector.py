"""Standalone Bitrix CRM identity source-record contracts."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from src.connectors.bitrix_crm.identity_connector import (
    BitrixCrmIdentityConnector,
    _company_envelope,
    _person_envelope,
)
from src.connectors.bitrix_openlines.models import CrmCompany, CrmContact
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import SourceRecordEnvelope


class _Client:
    def __init__(self) -> None:
        self.closed = False

    def iter_crm_contacts(self) -> Iterator[CrmContact]:
        yield CrmContact(
            id="101",
            full_name="Ada Lovelace",
            phones=("+6591234567",),
            emails=("ada@example.com",),
        )

    def iter_crm_leads(self) -> Iterator[CrmContact]:
        yield CrmContact(id="202", full_name="Grace Hopper", kind="lead")

    def iter_crm_companies(self) -> Iterator[CrmCompany]:
        yield CrmCompany(id="303", title="Analytical Engines")

    def close(self) -> None:
        self.closed = True


def test_standalone_connector_emits_portal_scoped_contacts_leads_and_company_references() -> None:
    client = _Client()
    connector = BitrixCrmIdentityConnector(
        client,
        BitrixOpenLinesConfig(source_instance_id="bitrix-primary"),
    )

    records = list(connector.fetch_records())
    envelopes = [
        SourceRecordEnvelope.model_validate({"source_system": connector.get_source_key(), **record})
        for record in records
    ]

    assert connector.get_source_key() == "bitrix_chat"
    assert [envelope.source_record_id for envelope in envelopes] == [
        "bitrix-crm-contact-101",
        "bitrix-crm-lead-202",
        "bitrix-crm-company-303",
    ]
    assert {envelope.source_instance_id for envelope in envelopes} == {"bitrix-primary"}
    assert envelopes[0].identifiers[0].type == "crm_contact_id"
    assert envelopes[1].identifiers[0].type == "crm_lead_id"
    assert envelopes[2].record_type.value == "crm_company"
    assert envelopes[2].identifiers == []
    assert envelopes[2].raw_payload["reference_metadata"] == {
        "identity_policy_version": "crm_company_reference_v1",
        "source_instance_id": "bitrix-primary",
        "crm_company_id": "303",
        "person_matching_prohibited": True,
    }
    assert envelopes[0].raw_payload["raw_identifier_group"] == [
        {"type": "crm_contact_id", "value": "101", "is_verified": True},
        {"type": "phone", "value": "+6591234567", "is_verified": False},
        {"type": "email", "value": "ada@example.com", "is_verified": False},
    ]
    assert all(len(envelope.record_hash) == 64 for envelope in envelopes)

    connector.close()
    assert client.closed is True


def test_standalone_connector_fails_closed_without_portal_registration() -> None:
    with pytest.raises(ValueError, match="requires source_instance_id"):
        BitrixCrmIdentityConnector(_Client(), BitrixOpenLinesConfig())


def test_standalone_record_hashes_include_mutable_source_profile_values() -> None:
    first_contact = _person_envelope(
        CrmContact(id="101", full_name="Ada Lovelace"),
        source_instance_id="bitrix-primary",
        entity_type="contact",
    )
    changed_contact = _person_envelope(
        CrmContact(id="101", full_name="Ada Byron"),
        source_instance_id="bitrix-primary",
        entity_type="contact",
    )
    first_company = _company_envelope(
        CrmCompany(id="303", title="Analytical Engines"),
        source_instance_id="bitrix-primary",
    )
    changed_company = _company_envelope(
        CrmCompany(id="303", title="Babbage Systems"),
        source_instance_id="bitrix-primary",
    )

    assert first_contact["record_hash"] != changed_contact["record_hash"]
    assert first_company["record_hash"] != changed_company["record_hash"]
