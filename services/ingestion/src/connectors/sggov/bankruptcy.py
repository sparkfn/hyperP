"""Connector for SG Bankruptcy Register dump records."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from src.connectors.base import SourceConnector
from src.connectors.sggov.bankruptcy_common import build_bankruptcy_envelope
from src.connectors.sggov.dump import CopyRow, iter_copy_rows
from src.models import JsonValue

_COPY_BATCH_SIZE = 1000


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


def _events_by_case(rows: list[CopyRow]) -> dict[str, CopyRow]:
    indexed: dict[str, CopyRow] = {}
    for row in rows:
        case_id = _str_value(row, "bankruptcy_case_id")
        if case_id is not None and (
            case_id not in indexed or _event_order_key(row) > _event_order_key(indexed[case_id])
        ):
            indexed[case_id] = row
    return indexed


def _event_order_key(row: CopyRow) -> tuple[str, int]:
    updated_at = _iso_datetime(row.get("updated_at")) or ""
    row_id = _str_value(row, "id") or "0"
    return updated_at, int(row_id) if row_id.isdigit() else 0


class SGGovernmentBankruptcyConnector(SourceConnector):
    """Yields one system source record per SG bankruptcy case."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "sgbankruptcy"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        cases = iter_copy_rows(self._dump_path, "bankruptcy_cases")
        for case_batch in _copy_row_batches(cases):
            yield from self._build_case_batch(case_batch)

    def _build_case_batch(
        self,
        cases: list[CopyRow],
    ) -> Iterator[dict[str, JsonValue]]:
        case_ids = {case_id for case in cases if (case_id := _str_value(case, "id")) is not None}
        events: dict[str, CopyRow] = {}
        for event in iter_copy_rows(self._dump_path, "case_events"):
            case_id = _str_value(event, "bankruptcy_case_id")
            if case_id not in case_ids:
                continue
            if case_id not in events or _event_order_key(event) > _event_order_key(events[case_id]):
                events[case_id] = event
        document_ids = {
            document_id
            for event in events.values()
            if (document_id := _str_value(event, "source_document_id")) is not None
        }
        documents = {
            document_id: document
            for document in iter_copy_rows(self._dump_path, "source_documents")
            if (document_id := _str_value(document, "id")) in document_ids
        }
        for case in cases:
            case_id = _str_value(case, "id")
            if case_id is None:
                continue
            latest_event = events.get(case_id)
            document = documents.get(_str_value(latest_event, "source_document_id") or "")
            yield self._build_envelope(case, latest_event, document)

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

        return build_bankruptcy_envelope(
            case_id=case_id,
            case_number=_str_value(case, "case_number"),
            identification_number=identification_number,
            person_name=person_name,
            latest_document_type=_str_value(case, "latest_document_type"),
            latest_document_date=_str_value(case, "latest_document_date"),
            first_seen_at=_iso_datetime(case.get("first_seen_at")),
            last_seen_at=_iso_datetime(case.get("last_seen_at")),
            event_id=_str_value(event, "id"),
            event_type=event_type,
            event_date=event_date,
            trustee_name=_str_value(event, "trustee_name"),
            trustee_firm=_str_value(event, "trustee_firm"),
            source_document_id=_str_value(document, "id"),
            source_url=_str_value(document, "source_url"),
            document_type=_str_value(document, "document_type"),
            document_date=_str_value(document, "document_date"),
        )


def _copy_row_batches(rows: Iterator[CopyRow]) -> Iterator[list[CopyRow]]:
    batch: list[CopyRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) == _COPY_BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch
