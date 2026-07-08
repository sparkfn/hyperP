from __future__ import annotations

from typing import cast

from _txmock import _RecordingTx
from neo4j import ManagedTransaction
from src.models import SourceRecordEnvelope
from src.pipeline_bankruptcy import materialize_bankruptcy_case


class _Tx(_RecordingTx):
    def __init__(self) -> None:
        super().__init__()

    def run(self, query: str, **kwargs: object) -> object:
        self._record(query, kwargs)
        return object()


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id="bankruptcy_case:1",
        observed_at="2026-05-05T13:05:42.351832+00:00",
        record_hash="sha256:abc",
        identifiers=[{"type": "nric", "value": "S9350236A", "is_verified": True}],
        attributes={"full_name": "SHARIFAH ALFIEYAH BINTE ABDULLAH"},
        raw_payload={
            "case": {
                "id": "1",
                "case_number": "1561/2025",
                "latest_document_type": "bankruptcy_order",
                "latest_document_date": "2026-02-26",
                "first_seen_at": "2026-05-05 13:05:42.10213+00",
                "last_seen_at": "2026-05-05 13:05:42.351832+00",
            },
            "event": {
                "event_type": "bankruptcy_order",
                "event_date": "2026-02-26",
                "trustee_name": "GOH WEE TECK",
                "trustee_firm": "RSM CORPORATE ADVISORY PTE. LTD.",
            },
            "source_document": {"source_url": "https://example.test/file.pdf"},
        },
    )


def test_materialize_bankruptcy_case_writes_case_for_sgbankruptcy() -> None:
    tx = _Tx()

    materialize_bankruptcy_case(
        cast(ManagedTransaction, tx),
        envelope=_envelope(),
        person_id="person-1",
        source_record_pk="sr-1",
    )

    assert len(tx.calls) == 1
    params = tx.calls[0][1]
    assert params["person_id"] == "person-1"
    assert params["source_record_pk"] == "sr-1"
    assert params["source_system_key"] == "sgbankruptcy"
    assert params["source_case_id"] == "1"
    assert params["case_number"] == "1561/2025"
    assert params["event_type"] == "bankruptcy_order"
    assert params["event_date"] == "2026-02-26"
    assert params["trustee_name"] == "GOH WEE TECK"
    assert params["source_url"] == "https://example.test/file.pdf"


def test_materialize_bankruptcy_case_skips_other_sources() -> None:
    tx = _Tx()
    envelope = _envelope().model_copy(update={"source_system": "fundbox_consumer_backend"})

    materialize_bankruptcy_case(
        cast(ManagedTransaction, tx),
        envelope=envelope,
        person_id="person-1",
        source_record_pk="sr-1",
    )

    assert tx.calls == []
