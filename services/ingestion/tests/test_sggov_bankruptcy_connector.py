from __future__ import annotations

from pathlib import Path

from src.connectors.sggov.bankruptcy import SGGovernmentBankruptcyConnector
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
    assert record["record_type"] == "system"
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


def test_bankruptcy_connector_is_registered() -> None:
    assert isinstance(get_connector("sgbankruptcy"), SGGovernmentBankruptcyConnector)
