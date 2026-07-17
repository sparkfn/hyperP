from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from src.connectors.dumps.connectors import get_dump_connector
from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector, _events_by_case
from src.connectors.sggov.bankruptcy_api import BankruptcyExportItem, build_api_envelope
from src.main import get_connector


def _line(values: list[str]) -> str:
    return "\t".join(values) + "\n"


def _write_dump(path: Path) -> None:
    path.write_text(
        "COPY public.bankruptcy_cases "
        "(id, case_number, identification_number, person_name, first_seen_at, last_seen_at, "
        "latest_document_type, latest_document_date) FROM stdin;\n"
        + _line(
            [
                "1",
                "1561/2025",
                "S9350236A",
                "SHARIFAH ALFIEYAH BINTE ABDULLAH",
                "2026-05-05 13:05:42.10213+00",
                "2026-05-05 13:05:42.351832+00",
                "bankruptcy_order",
                "2026-02-26",
            ]
        )
        + "\\.\n"
        + "COPY public.case_events "
        "(id, bankruptcy_case_id, source_document_id, event_type, event_date, "
        "identification_number, person_name, trustee_name, trustee_firm, raw_text, "
        "parsed_payload_json, created_at, updated_at) FROM stdin;\n"
        + _line(
            [
                "7",
                "1",
                "3",
                "bankruptcy_order",
                "2026-02-26",
                "S9350236A",
                "SHARIFAH ALFIEYAH BINTE ABDULLAH",
                "GOH WEE TECK",
                "RSM CORPORATE ADVISORY PTE. LTD.",
                "1561/2025\\nS9350236A",
                "{}",
                "2026-05-05 13:05:42.10213+00",
                "2026-05-05 13:05:42.362259+00",
            ]
        )
        + "\\.\n"
        + "COPY public.source_documents "
        "(id, source_page_id, document_type, source_url, raw_href, link_text, week_label, "
        "week_number, week_suffix, document_date, is_new, filename, local_path, "
        "content_sha256, first_seen_at, last_seen_at, downloaded_at, extraction_status, "
        "extracted_at, extraction_error) FROM stdin;\n"
        + _line(
            [
                "3",
                "1",
                "bankruptcy_order",
                "https://example.test/file.pdf",
                "/file.pdf",
                "Bankruptcy Orders",
                "15",
                "15",
                "\\N",
                "2026-02-26",
                "f",
                "file.pdf",
                "/data/file.pdf",
                "abc123",
                "2026-05-05 13:00:00+00",
                "2026-05-05 13:00:00+00",
                "2026-05-05 13:00:01+00",
                "success",
                "2026-05-05 13:05:00+00",
                "\\N",
            ]
        )
        + "\\.\n",
        encoding="utf-8",
    )


def test_bankruptcy_connector_yields_case_envelope(tmp_path: Path) -> None:
    dump = tmp_path / "sgbankruptcy.sql"
    _write_dump(dump)

    connector = SGGovernmentBankruptcyConnector(dump_path=dump)
    records = list(connector.fetch_records())

    assert len(records) == 1
    record = records[0]
    assert record["source_record_id"] == "bankruptcy_case:1"
    assert record["observed_at"] == "2026-05-05T13:05:42.351832+00:00"
    assert record["record_type"] == "bankruptcy"
    assert record["identifiers"] == [{"type": "nric", "value": "S9350236A", "is_verified": True}]
    assert isinstance(record["attributes"], dict)
    attributes = record["attributes"]
    assert attributes["full_name"] == "SHARIFAH ALFIEYAH BINTE ABDULLAH"
    assert attributes["bankruptcy_case_number"] == "1561/2025"
    assert isinstance(record["raw_payload"], dict)
    raw_payload = record["raw_payload"]
    assert isinstance(raw_payload["case"], dict)
    assert isinstance(raw_payload["event"], dict)
    assert isinstance(raw_payload["source_document"], dict)
    assert raw_payload["case"]["case_number"] == "1561/2025"
    assert raw_payload["event"]["trustee_name"] == "GOH WEE TECK"
    assert raw_payload["source_document"]["source_url"] == "https://example.test/file.pdf"
    assert isinstance(record["record_hash"], str)
    assert record["record_hash"].startswith("sha256:")


def test_bankruptcy_connector_dispatches_via_dump(tmp_path: Path) -> None:
    dump = tmp_path / "sgbankruptcy.sql"
    _write_dump(dump)
    assert isinstance(get_dump_connector("sgbankruptcy", dump), SGGovernmentBankruptcyConnector)


def test_bankruptcy_connector_not_registered_for_batch() -> None:
    # SG is dump-only: the dump_path must be supplied via the task call, so it
    # is not available in batch mode.
    with pytest.raises(ValueError, match="Unknown source key"):
        get_connector("sgbankruptcy")


def test_bankruptcy_connector_dispatches_via_api(monkeypatch: pytest.MonkeyPatch) -> None:
    marker = object()
    monkeypatch.setattr("src.main.create_sgbankruptcy_api_connector", lambda: marker)

    assert get_connector("sgbankruptcy", mode="api") is marker


def test_bankruptcy_api_and_dump_connectors_build_identical_envelopes(tmp_path: Path) -> None:
    dump = tmp_path / "sgbankruptcy.sql"
    _write_dump(dump)
    dump_record = next(SGGovernmentBankruptcyConnector(dump).fetch_records())
    api_record = build_api_envelope(
        BankruptcyExportItem(
            case_id=1,
            case_number="1561/2025",
            identification_number="S9350236A",
            person_name="SHARIFAH ALFIEYAH BINTE ABDULLAH",
            latest_document_type="bankruptcy_order",
            latest_document_date="2026-02-26",
            event_id=7,
            event_type="bankruptcy_order",
            event_date="2026-02-26",
            trustee_name="GOH WEE TECK",
            trustee_firm="RSM CORPORATE ADVISORY PTE. LTD.",
            source_document_id=3,
            source_url="https://example.test/file.pdf",
            document_type="bankruptcy_order",
            document_date="2026-02-26",
            first_seen_at=datetime.fromisoformat("2026-05-05T13:05:42.102130+00:00"),
            last_seen_at=datetime.fromisoformat("2026-05-05T13:05:42.351832+00:00"),
        )
    )

    assert api_record == dump_record


def test_dump_connector_selects_latest_event_per_case() -> None:
    events = _events_by_case(
        [
            {"id": "9", "bankruptcy_case_id": "1", "updated_at": "2026-07-01T12:00:00"},
            {"id": "10", "bankruptcy_case_id": "1", "updated_at": "2026-07-02T12:00:00"},
        ]
    )

    assert events["1"]["id"] == "10"
