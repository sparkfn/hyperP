"""Contracts for standalone Bitrix contact, lead, and company identity."""

from __future__ import annotations

import pytest
from src.connectors.bitrix_openlines.crm_identity_policy import (
    CRM_COMPANY_REFERENCE_POLICY_VERSION,
    CRM_CONTACT_IDENTITY_POLICY_VERSION,
    CRM_DEAL_IDENTITY_POLICY_VERSION,
    CRM_LEAD_IDENTITY_POLICY_VERSION,
    MAX_CRM_CONTACT_PHONES,
    crm_company_reference_evidence,
    crm_contact_identity_evidence,
    crm_standalone_contact_identity_evidence,
    crm_standalone_lead_identity_evidence,
)
from src.connectors.bitrix_openlines.models import CrmCompany, CrmContact


def test_standalone_contact_uses_its_own_policy_version() -> None:
    contact = CrmContact(
        id="123",
        full_name="Ada Lovelace",
        phones=("+6591234567",),
        emails=("ada@example.com",),
    )

    evidence = crm_standalone_contact_identity_evidence(
        contact, source_instance_id="bitrix-primary"
    )

    assert evidence.identifiers == (
        {"type": "crm_contact_id", "value": "123", "is_verified": True},
        {"type": "phone", "value": "+6591234567", "is_verified": False},
        {"type": "email", "value": "ada@example.com", "is_verified": False},
    )
    assert evidence.source_instance_id == "bitrix-primary"
    assert evidence.metadata["identity_policy_version"] == CRM_CONTACT_IDENTITY_POLICY_VERSION
    assert evidence.metadata["source_instance_id"] == "bitrix-primary"
    assert evidence.metadata["crm_contact_id"] == "123"


def test_standalone_lead_never_reuses_external_customer_id() -> None:
    evidence = crm_standalone_lead_identity_evidence(
        CrmContact(id="456", full_name=None, kind="lead"),
        source_instance_id="bitrix-primary",
    )

    assert evidence.identifiers == ({"type": "crm_lead_id", "value": "456", "is_verified": True},)
    assert evidence.source_instance_id == "bitrix-primary"
    assert evidence.metadata["identity_policy_version"] == CRM_LEAD_IDENTITY_POLICY_VERSION
    assert evidence.metadata["source_instance_id"] == "bitrix-primary"
    assert evidence.metadata["crm_lead_id"] == "456"
    assert "crm_contact_id" not in evidence.metadata


def test_standalone_contact_suppresses_all_channels_when_one_array_is_oversized() -> None:
    evidence = crm_standalone_contact_identity_evidence(
        CrmContact(
            id="123",
            full_name=None,
            phones=tuple(f"+65912345{index:02d}" for index in range(MAX_CRM_CONTACT_PHONES + 1)),
            emails=("ada@example.com",),
        ),
        source_instance_id="bitrix-primary",
    )

    assert evidence.identifiers == (
        {"type": "crm_contact_id", "value": "123", "is_verified": True},
    )
    assert evidence.metadata["channel_hints_suppressed"] is True
    assert evidence.metadata["channel_hint_suppression_reasons"] == ["phone_cardinality_exceeded"]


def test_company_is_only_a_non_person_source_reference() -> None:
    evidence = crm_company_reference_evidence(
        CrmCompany(id="789", title="Analytical Engines"),
        source_instance_id="bitrix-primary",
    )

    assert evidence.reference == {"type": "crm_company_id", "value": "789"}
    assert evidence.source_instance_id == "bitrix-primary"
    assert evidence.metadata == {
        "identity_policy_version": CRM_COMPANY_REFERENCE_POLICY_VERSION,
        "source_instance_id": "bitrix-primary",
        "crm_company_id": "789",
        "person_matching_prohibited": True,
    }
    assert not hasattr(evidence, "identifiers")


def test_deal_v2_policy_output_remains_unchanged() -> None:
    contact = CrmContact(id="456", full_name=None, kind="lead")

    evidence = crm_contact_identity_evidence(contact)

    assert evidence.identifiers == (
        {"type": "external_customer_id", "value": "456", "is_verified": True},
    )
    assert evidence.metadata["identity_policy_version"] == CRM_DEAL_IDENTITY_POLICY_VERSION


def test_standalone_policy_rejects_the_wrong_record_kind() -> None:
    with pytest.raises(ValueError, match="requires a contact"):
        crm_standalone_contact_identity_evidence(
            CrmContact(id="123", full_name=None, kind="lead"),
            source_instance_id="bitrix-primary",
        )

    with pytest.raises(ValueError, match="requires a lead"):
        crm_standalone_lead_identity_evidence(
            CrmContact(id="123", full_name=None),
            source_instance_id="bitrix-primary",
        )


def test_standalone_evidence_requires_an_explicit_source_instance() -> None:
    contact = CrmContact(id="123", full_name=None)

    with pytest.raises(ValueError, match="canonical non-secret slug"):
        crm_standalone_contact_identity_evidence(contact, source_instance_id="  ")

    with pytest.raises(ValueError, match="canonical non-secret slug"):
        crm_company_reference_evidence(CrmCompany(id="789", title=None), source_instance_id="")
