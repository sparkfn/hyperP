"""SG bankruptcy ingestion is match-only: no new persons, unmatched parked."""

from __future__ import annotations

from typing import cast

from _txmock import _RecordingTx, summary_counter_result
from neo4j import ManagedTransaction
from src.graph import queries
from src.graph.queries.source_records import FIND_PARKED_SOURCE_RECORD_BY_HASH
from src.models import SourceRecordEnvelope
from src.pipeline import IngestPipeline, _is_match_only_source

_EMPTY_LOCK_STATE: dict[str, object] = {
    "source_record_pk": None,
    "source_record_version": None,
    "record_hash": None,
    "lifecycle_status": None,
    "linked_person_ids": [],
    "max_source_record_version": None,
}


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
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        self._record(query, kwargs)
        if "max_source_record_version" in query:
            return _Result(dict(_EMPTY_LOCK_STATE))
        if "ACTIVATE" not in query and "pending.lifecycle_status = 'active'" in query:
            return _Result({"source_record_pk": "sr-1"})
        if "CREATE (sr:SourceRecord" in query:
            return _Result({"source_record_pk": "sr-parked"})
        return _Result()


class _ParkedDuplicateTx(_RecordingTx):
    """An identical parked version already exists for this source identity."""

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        if query == FIND_PARKED_SOURCE_RECORD_BY_HASH:
            return _Result({"source_record_pk": "sr-parked"})
        if "max_source_record_version" in query:
            return _Result(dict(_EMPTY_LOCK_STATE))
        return _Result()


class _MatchedTx(_RecordingTx):
    """The incoming NRIC is owned by an existing person-1 with a VALID edge."""

    def run(self, query: str, **kwargs: object) -> _Result:
        counter = summary_counter_result(query)
        if counter is not None:
            return counter
        self._record(query, kwargs)
        if "max_source_record_version" in query:
            return _Result(
                {
                    "source_record_pk": None,
                    "source_record_version": None,
                    "record_hash": None,
                    "lifecycle_status": None,
                    "linked_person_ids": [],
                    "max_source_record_version": None,
                }
            )
        if "pending.lifecycle_status = 'active'" in query:
            return _Result({"source_record_pk": "sr-1"})
        if "person_id AS person_id" in query and "rel.quality_flag = 'valid'" in query:
            return _Result({"person_id": "person-1"})
        if "input.input_index AS input_index" in query:
            return _Result(
                rows=[
                    {
                        "input_index": 0,
                        "identifier_type": "nric",
                        "normalized_value": "S1234567A",
                        "fanout": 1,
                        "person_ids": ["person-1"],
                    }
                ]
            )
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

    def __enter__(self) -> _Session:
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
    assert _is_match_only_source("fundbox") is False


def test_unmatched_bankruptcy_record_is_persisted_parked_without_identity_writes() -> None:
    tx = _Tx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.parked is True
    assert result.person_id is None
    assert result.source_record_pk == "sr-parked"
    issued = [q for q, _ in tx.calls]
    assert not any("CREATE (p:Person" in q or "MERGE (p:Person" in q for q in issued)
    assert queries.CREATE_MATCH_DECISION not in issued
    assert queries.LINK_SOURCE_RECORD_TO_PERSON not in issued
    assert queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION not in issued
    creates = [params for query, params in tx.calls if query == queries.CREATE_SOURCE_RECORD]
    assert len(creates) == 1
    assert creates[0]["lifecycle_status"] == "parked"
    assert creates[0]["link_status"] == "unlinked"
    assert creates[0]["expected_active_source_record_pk"] is None


def test_rerun_of_a_parked_record_is_a_duplicate_without_writes() -> None:
    tx = _ParkedDuplicateTx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.skipped_duplicate is True
    assert result.parked is True
    assert result.source_record_pk == "sr-parked"
    assert not any(query == queries.CREATE_SOURCE_RECORD for query, _ in tx.calls)


def test_matched_bankruptcy_record_links_to_existing_person() -> None:
    tx = _MatchedTx()
    client = _Client(tx)
    pipeline = IngestPipeline(cast(object, client))

    result = pipeline.ingest(_bankruptcy_envelope(), ingest_run_id="run-1")

    assert result.parked is False
    assert result.person_id == "person-1"
    assert result.is_new_person is False
