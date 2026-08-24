"""Conservative identity-evidence policy for Bitrix CRM deals.

CRM contacts are useful for resolving a deal, but their PHONE and EMAIL fields
are not reliable enough to become verified Person identity evidence. In
particular, imported aggregate contacts can carry large unrelated channel
arrays. This module keeps the canonical CRM key while bounding channel hints.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.connectors.bitrix_openlines.models import CrmContact
from src.models import JsonValue

CRM_DEAL_IDENTITY_POLICY_VERSION = "crm_deal_identity_v2"
MAX_CRM_CONTACT_PHONES = 5
MAX_CRM_CONTACT_EMAILS = 5


@dataclass(frozen=True)
class CrmContactIdentityEvidence:
    """Canonical CRM identity plus bounded, match-only channel hints."""

    identifiers: tuple[dict[str, JsonValue], ...]
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
