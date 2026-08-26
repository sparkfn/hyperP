"""Standalone Bitrix CRM identity source-record contracts."""

from __future__ import annotations

from src.connectors.bitrix_crm.identity_connector import _company_envelope, _person_envelope
from src.connectors.bitrix_openlines.models import CrmCompany, CrmContact


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
