"""Bankruptcy-specific graph materialization for SG Gov ingestion."""

from __future__ import annotations

import json
from datetime import datetime

from neo4j import ManagedTransaction

from src.graph import queries
from src.models import JsonValue, SourceRecordEnvelope


def _row(payload: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _str_value(row: dict[str, JsonValue], key: str) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _iso_datetime(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized = f"{normalized}:00"
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return normalized


def bankruptcy_case_blueprint(envelope: SourceRecordEnvelope) -> dict[str, JsonValue] | None:
    """Return the service-neutral projection persisted for later review activation."""
    if envelope.source_system != "sgbankruptcy":
        return None
    case = _row(envelope.raw_payload, "case")
    event = _row(envelope.raw_payload, "event")
    document = _row(envelope.raw_payload, "source_document")
    source_case_id = _str_value(case, "id")
    if source_case_id is None:
        return None
    return {
        "source_system_key": envelope.source_system,
        "source_case_id": source_case_id,
        "case_number": _str_value(case, "case_number"),
        "document_type": _str_value(case, "latest_document_type")
        or _str_value(document, "document_type"),
        "document_date": _str_value(case, "latest_document_date")
        or _str_value(document, "document_date"),
        "event_type": _str_value(event, "event_type"),
        "event_date": _str_value(event, "event_date"),
        "trustee_name": _str_value(event, "trustee_name"),
        "trustee_firm": _str_value(event, "trustee_firm"),
        "source_url": _str_value(document, "source_url"),
        "first_seen_at": _iso_datetime(_str_value(case, "first_seen_at")),
        "last_seen_at": _iso_datetime(_str_value(case, "last_seen_at")),
        "observed_at": envelope.observed_at,
        "raw_payload": json.dumps(envelope.raw_payload, default=str),
    }


def materialize_bankruptcy_case(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    person_id: str,
    source_record_pk: str,
    replaced_source_record_pk: str | None,
) -> None:
    if envelope.source_system != "sgbankruptcy":
        return

    if replaced_source_record_pk is not None:
        tx.run(
            queries.RETIRE_BANKRUPTCY_PERSON_ASSOCIATION,
            source_record_pk=replaced_source_record_pk,
        )

    case = _row(envelope.raw_payload, "case")
    event = _row(envelope.raw_payload, "event")
    document = _row(envelope.raw_payload, "source_document")
    source_case_id = _str_value(case, "id")
    if source_case_id is None:
        return

    tx.run(
        queries.MERGE_BANKRUPTCY_CASE,
        person_id=person_id,
        source_record_pk=source_record_pk,
        source_system_key=envelope.source_system,
        source_case_id=source_case_id,
        case_number=_str_value(case, "case_number"),
        document_type=(
            _str_value(case, "latest_document_type") or _str_value(document, "document_type")
        ),
        document_date=(
            _str_value(case, "latest_document_date") or _str_value(document, "document_date")
        ),
        event_type=_str_value(event, "event_type"),
        event_date=_str_value(event, "event_date"),
        trustee_name=_str_value(event, "trustee_name"),
        trustee_firm=_str_value(event, "trustee_firm"),
        source_url=_str_value(document, "source_url"),
        first_seen_at=_iso_datetime(_str_value(case, "first_seen_at")),
        last_seen_at=_iso_datetime(_str_value(case, "last_seen_at")),
        observed_at=envelope.observed_at,
        raw_payload=json.dumps(envelope.raw_payload, default=str),
    )
