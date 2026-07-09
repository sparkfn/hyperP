"""SG bankruptcy ingestion is match-only: no new persons, unmatched dropped."""

from __future__ import annotations

from typing import cast

from _txmock import _RecordingTx
from neo4j import ManagedTransaction
from src.models import SourceRecordEnvelope
from src.pipeline import IngestPipeline, _is_match_only_source


class _Result:
    def __init__(
        self,
        row: dict[str, object] | None = None,
        rows: list[dict[str, object]] | None = None,
    ) -> None:
        self._row = row
        self._rows = rows or ([] if row is None else [row])

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> object:
        return iter(self._rows)


class _Tx(_RecordingTx):
    """No candidate ever owns the incoming NRIC — nothing matches."""

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        return _Result()


class _MatchedTx(_RecordingTx):
    """The incoming NRIC is owned by an existing person-1 with a VALID edge."""

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        if "person_id AS person_id" in query and "rel.quality_flag = 'valid'" in query:
            return _Result({"person_id": "person-1"})
        if "candidate:Person" in query:
            return _Result(rows=[{"person_id": "person-1"}])
        if "RETURN sr.source_record_pk AS source_record_pk" in query:
            return _Result({"source_record_pk": "sr-1"})
        if "match_decision_id" in query:
            return _Result({"match_decision_id": "md-1"})
        if "merge_event_id" in query:
            return _Result({"merge_event_id": "me-1"})
        return _Result()


class _Session:
    def __init__(self, tx: _RecordingTx) -> None:
        self.tx = tx

    def __enter__(self) -> "_Session":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[operator]


class _Client:
    def __init__(self, tx: _RecordingTx) -> None:
        self._tx = tx

    def session(self) -> _Session:
        return _Session(self._tx)

    def execute_read(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self._tx))  # type: ignore[operator]


def _bankruptcy_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgbankruptcy",
        source_record_id="bankruptcy_case:1",
        record_type="bankruptcy",
        observed_at="2026-05-08T09:47:25+00:00",
        record_hash="sha256:abc",
        identifiers=[{"type": "nric", "value": "S1234567A", "is_verified": True}],
        attributes={"full_name": "Ada Lovelace"},
        raw_payload={"case": {"id": "1"}},
    )


def test_sgbankruptcy_is_match_only_source() -> None:
    assert _is_match_only_source("sgbankruptcy") is True
    assert _is_match_only_source("sgrentalflats") is False
    assert _is_match_only_source("fundbox_consumer_backend") is False


def test_unmatched_bankruptcy_record_is_dropped_with_no_writes() -> None:
    tx = _Tx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.dropped is True
    assert result.person_id is None
    all_queries = [q for q, _ in tx.calls]
    assert not any("CREATE (p:Person" in q or "MERGE (p:Person" in q for q in all_queries)
    assert not any("CREATE (sr:SourceRecord" in q for q in all_queries)


def test_matched_bankruptcy_record_links_to_existing_person() -> None:
    tx = _MatchedTx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.dropped is False
    assert result.person_id == "person-1"
    assert result.is_new_person is False
