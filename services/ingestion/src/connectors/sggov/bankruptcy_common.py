"""Canonical envelope construction shared by SG bankruptcy connectors."""

from __future__ import annotations

from src.connectors.fundbox.builders import IdentifierBag, build_envelope
from src.models import JsonValue


def build_bankruptcy_envelope(
    *,
    case_id: str,
    case_number: str | None,
    identification_number: str | None,
    person_name: str | None,
    latest_document_type: str | None,
    latest_document_date: str | None,
    first_seen_at: str | None,
    last_seen_at: str | None,
    event_id: str | None,
    event_type: str | None,
    event_date: str | None,
    trustee_name: str | None,
    trustee_firm: str | None,
    source_document_id: str | None,
    source_url: str | None,
    document_type: str | None,
    document_date: str | None,
) -> dict[str, JsonValue]:
    """Build a mode-independent bankruptcy source-record envelope."""
    identifiers = IdentifierBag()
    identifiers.add("nric", identification_number, verified=True)
    canonical_event_type = event_type or latest_document_type
    canonical_event_date = event_date or latest_document_date
    event_payload: dict[str, JsonValue] = {}
    if event_id is not None:
        event_payload = {
            "id": event_id,
            "event_type": event_type,
            "event_date": event_date,
            "trustee_name": trustee_name,
            "trustee_firm": trustee_firm,
        }
    document_payload: dict[str, JsonValue] = {}
    if source_document_id is not None:
        document_payload = {
            "id": source_document_id,
            "source_url": source_url,
            "document_type": document_type,
            "document_date": document_date,
        }
    return build_envelope(
        source_record_id=f"bankruptcy_case:{case_id}",
        observed_at=last_seen_at or first_seen_at,
        identifiers=identifiers.items,
        record_type="bankruptcy",
        attributes={
            "full_name": person_name,
            "bankruptcy_case_number": case_number,
            "bankruptcy_document_type": latest_document_type,
            "bankruptcy_document_date": latest_document_date,
            "bankruptcy_event_type": canonical_event_type,
            "bankruptcy_event_date": canonical_event_date,
            "bankruptcy_trustee_name": trustee_name,
            "bankruptcy_trustee_firm": trustee_firm,
        },
        raw_payload={
            "case": {
                "id": case_id,
                "case_number": case_number,
                "identification_number": identification_number,
                "person_name": person_name,
                "latest_document_type": latest_document_type,
                "latest_document_date": latest_document_date,
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
            },
            "event": event_payload,
            "source_document": document_payload,
        },
    )
