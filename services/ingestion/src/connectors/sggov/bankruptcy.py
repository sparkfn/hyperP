"""Connector for SG Bankruptcy Register dump records."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import IdentifierBag, build_envelope
from src.connectors.sggov.dump import CopyRow, parse_copy_tables
from src.models import JsonValue

_DEFAULT_DUMP_PATH = Path(".dumps/sgbankruptcy_2026-05-11.sql")


def _iso_datetime(value: JsonValue) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace(" ", "T", 1)
    if normalized.endswith("+00"):
        normalized = f"{normalized}:00"
    try:
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError:
        return normalized


def _str_value(row: CopyRow | None, key: str) -> str | None:
    if row is None:
        return None
    value = row.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _index_by_id(rows: list[CopyRow]) -> dict[str, CopyRow]:
    indexed: dict[str, CopyRow] = {}
    for row in rows:
        row_id = _str_value(row, "id")
        if row_id is not None:
            indexed[row_id] = row
    return indexed


def _events_by_case(rows: list[CopyRow]) -> dict[str, CopyRow]:
    indexed: dict[str, CopyRow] = {}
    for row in rows:
        case_id = _str_value(row, "bankruptcy_case_id")
        if case_id is not None and case_id not in indexed:
            indexed[case_id] = row
    return indexed


class SGGovernmentBankruptcyConnector(SourceConnector):
    """Yields one system source record per SG bankruptcy case."""

    def __init__(self, dump_path: Path = _DEFAULT_DUMP_PATH) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "sgbankruptcy"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = parse_copy_tables(
            self._dump_path,
            {"bankruptcy_cases", "case_events", "source_documents"},
        )
        cases = tables.get("bankruptcy_cases", [])
        events = _events_by_case(tables.get("case_events", []))
        documents = _index_by_id(tables.get("source_documents", []))

        for case in cases:
            case_id = _str_value(case, "id")
            if case_id is None:
                continue
            event = events.get(case_id)
            document = documents.get(_str_value(event, "source_document_id") or "")
            yield self._build_envelope(case, event, document)

    def _build_envelope(
        self,
        case: CopyRow,
        event: CopyRow | None,
        document: CopyRow | None,
    ) -> dict[str, JsonValue]:
        case_id = _str_value(case, "id") or "unknown"
        identification_number = _str_value(case, "identification_number") or _str_value(
            event, "identification_number"
        )
        person_name = _str_value(case, "person_name") or _str_value(event, "person_name")
        event_type = _str_value(event, "event_type") or _str_value(case, "latest_document_type")
        event_date = _str_value(event, "event_date") or _str_value(case, "latest_document_date")

        identifiers = IdentifierBag()
        identifiers.add("nric", identification_number, verified=True)

        raw_payload: dict[str, JsonValue] = {
            "case": case,
            "event": event or {},
            "source_document": document or {},
        }
        observed_at = _iso_datetime(case.get("last_seen_at")) or _iso_datetime(
            case.get("first_seen_at")
        )
        return build_envelope(
            source_record_id=f"bankruptcy_case:{case_id}",
            observed_at=observed_at,
            identifiers=identifiers.items,
            attributes={
                "full_name": person_name,
                "bankruptcy_case_number": _str_value(case, "case_number"),
                "bankruptcy_document_type": _str_value(case, "latest_document_type"),
                "bankruptcy_document_date": _str_value(case, "latest_document_date"),
                "bankruptcy_event_type": event_type,
                "bankruptcy_event_date": event_date,
                "bankruptcy_trustee_name": _str_value(event, "trustee_name"),
                "bankruptcy_trustee_firm": _str_value(event, "trustee_firm"),
            },
            raw_payload=raw_payload,
        )
