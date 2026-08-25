"""Versioned identity-evidence policies for Bitrix CRM records.

The existing deal-v2 contract remains immutable. Standalone contacts and leads
use independent portal-scoped contracts, while companies produce references
that cannot enter Person matching.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.connectors.bitrix_openlines.models import CrmCompany, CrmContact
from src.models import JsonValue
from src.source_instances import canonical_source_instance_id

CRM_DEAL_IDENTITY_POLICY_VERSION = "crm_deal_identity_v2"
CRM_CONTACT_IDENTITY_POLICY_VERSION = "crm_contact_identity_v1"
CRM_LEAD_IDENTITY_POLICY_VERSION = "crm_lead_identity_v1"
CRM_COMPANY_REFERENCE_POLICY_VERSION = "crm_company_reference_v1"
MAX_CRM_CONTACT_PHONES = 5
MAX_CRM_CONTACT_EMAILS = 5


@dataclass(frozen=True)
class CrmContactIdentityEvidence:
    """Canonical CRM identity plus bounded, match-only channel hints."""

    identifiers: tuple[dict[str, JsonValue], ...]
    metadata: dict[str, JsonValue]


@dataclass(frozen=True)
class CrmStandaloneIdentityEvidence:
    """Portal-scoped, Person-eligible evidence for one standalone CRM record."""

    source_instance_id: str
    identifiers: tuple[dict[str, JsonValue], ...]
    metadata: dict[str, JsonValue]


@dataclass(frozen=True)
class CrmCompanyReferenceEvidence:
    """Portal-scoped company reference that must not enter Person matching."""

    source_instance_id: str
    reference: dict[str, JsonValue]
    metadata: dict[str, JsonValue]


def crm_contact_identity_evidence(contact: CrmContact) -> CrmContactIdentityEvidence:
    """Build safe deal identity evidence for one hydrated CRM contact.

    The canonical CRM identifier is always retained. If either channel array
    exceeds its conservative cardinality limit, both channel arrays are
    excluded so a polluted contact cannot auto-resolve through a subset.
    """
    phone_count = len(contact.phones)
    email_count = len(contact.emails)
    oversized_phone = phone_count > MAX_CRM_CONTACT_PHONES
    oversized_email = email_count > MAX_CRM_CONTACT_EMAILS
    channels_suppressed = oversized_phone or oversized_email
    reasons: list[JsonValue] = []
    if oversized_phone:
        reasons.append("phone_cardinality_exceeded")
    if oversized_email:
        reasons.append("email_cardinality_exceeded")

    identifiers: list[dict[str, JsonValue]] = [
        {
            "type": "crm_contact_id" if contact.kind == "contact" else "external_customer_id",
            "value": contact.id,
            "is_verified": True,
        }
    ]
    if not channels_suppressed:
        identifiers.extend(
            {"type": "phone", "value": value, "is_verified": False} for value in contact.phones
        )
        identifiers.extend(
            {"type": "email", "value": value, "is_verified": False} for value in contact.emails
        )
    return CrmContactIdentityEvidence(
        identifiers=tuple(identifiers),
        metadata={
            "identity_policy_version": CRM_DEAL_IDENTITY_POLICY_VERSION,
            "crm_contact_id": contact.id,
            "crm_contact_kind": contact.kind,
            "phone_count": phone_count,
            "email_count": email_count,
            "max_phone_count": MAX_CRM_CONTACT_PHONES,
            "max_email_count": MAX_CRM_CONTACT_EMAILS,
            "channel_hints_suppressed": channels_suppressed,
            "channel_hint_suppression_reasons": reasons,
        },
    )


def crm_standalone_contact_identity_evidence(
    contact: CrmContact,
    *,
    source_instance_id: str,
) -> CrmStandaloneIdentityEvidence:
    """Build independent-contact evidence without changing the deal-v2 contract."""
    if contact.kind != "contact":
        raise ValueError("Standalone CRM contact evidence requires a contact")
    return _standalone_identity_evidence(
        contact,
        source_instance_id=source_instance_id,
        identifier_type="crm_contact_id",
        record_id_metadata_key="crm_contact_id",
        policy_version=CRM_CONTACT_IDENTITY_POLICY_VERSION,
    )


def crm_standalone_lead_identity_evidence(
    lead: CrmContact,
    *,
    source_instance_id: str,
) -> CrmStandaloneIdentityEvidence:
    """Build independent-lead evidence using the lead-specific identifier namespace."""
    if lead.kind != "lead":
        raise ValueError("Standalone CRM lead evidence requires a lead")
    return _standalone_identity_evidence(
        lead,
        source_instance_id=source_instance_id,
        identifier_type="crm_lead_id",
        record_id_metadata_key="crm_lead_id",
        policy_version=CRM_LEAD_IDENTITY_POLICY_VERSION,
    )


def crm_company_reference_evidence(
    company: CrmCompany,
    *,
    source_instance_id: str,
) -> CrmCompanyReferenceEvidence:
    """Build a portal-scoped reference that cannot become Person identity evidence."""
    normalized_instance_id = canonical_source_instance_id(source_instance_id)
    return CrmCompanyReferenceEvidence(
        source_instance_id=normalized_instance_id,
        reference={"type": "crm_company_id", "value": company.id},
        metadata={
            "identity_policy_version": CRM_COMPANY_REFERENCE_POLICY_VERSION,
            "source_instance_id": normalized_instance_id,
            "crm_company_id": company.id,
            "person_matching_prohibited": True,
        },
    )


def _standalone_identity_evidence(
    contact: CrmContact,
    *,
    source_instance_id: str,
    identifier_type: str,
    record_id_metadata_key: str,
    policy_version: str,
) -> CrmStandaloneIdentityEvidence:
    normalized_instance_id = canonical_source_instance_id(source_instance_id)
    phone_count = len(contact.phones)
    email_count = len(contact.emails)
    oversized_phone = phone_count > MAX_CRM_CONTACT_PHONES
    oversized_email = email_count > MAX_CRM_CONTACT_EMAILS
    channels_suppressed = oversized_phone or oversized_email
    reasons: list[JsonValue] = []
    if oversized_phone:
        reasons.append("phone_cardinality_exceeded")
    if oversized_email:
        reasons.append("email_cardinality_exceeded")

    identifiers: list[dict[str, JsonValue]] = [
        {"type": identifier_type, "value": contact.id, "is_verified": True}
    ]
    if not channels_suppressed:
        identifiers.extend(
            {"type": "phone", "value": value, "is_verified": False} for value in contact.phones
        )
        identifiers.extend(
            {"type": "email", "value": value, "is_verified": False} for value in contact.emails
        )
    return CrmStandaloneIdentityEvidence(
        source_instance_id=normalized_instance_id,
        identifiers=tuple(identifiers),
        metadata={
            "identity_policy_version": policy_version,
            "source_instance_id": normalized_instance_id,
            record_id_metadata_key: contact.id,
            "crm_record_kind": contact.kind,
            "phone_count": phone_count,
            "email_count": email_count,
            "max_phone_count": MAX_CRM_CONTACT_PHONES,
            "max_email_count": MAX_CRM_CONTACT_EMAILS,
            "channel_hints_suppressed": channels_suppressed,
            "channel_hint_suppression_reasons": reasons,
        },
    )
