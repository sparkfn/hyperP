from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from _txmock import _RecordingTx
from neo4j import ManagedTransaction
from src.graph import queries
from src.main import _is_address_only_source
from src.models import IngestResult, NormalizedAddress, QualityFlag, SourceRecordEnvelope
from src.pipeline_addresses import ingest_address_record
from src.pipeline_writes import link_record_to_graph, link_source_record_to_address
from src.source_version_keys import encode_source_version_key


def test_address_upsert_matches_by_full_normalized_key() -> None:
    assert "MERGE (addr:Address" in queries.UPSERT_ADDRESS
    assert "country_code:  $country_code" in queries.UPSERT_ADDRESS
    assert "postal_code:   $postal_code" in queries.UPSERT_ADDRESS
    assert "street_name:   $street_name" in queries.UPSERT_ADDRESS
    assert "street_number: $street_number" in queries.UPSERT_ADDRESS
    assert "unit_number:   $unit_number" in queries.UPSERT_ADDRESS
    assert "OPTIONAL MATCH (existing:Address" not in queries.UPSERT_ADDRESS


def test_describe_address_query_is_exported() -> None:
    assert "DESCRIBES_ADDRESS" in queries.LINK_SOURCE_RECORD_TO_ADDRESS


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter([] if self._row is None else [self._row])


class _Tx(_RecordingTx):
    def __init__(self) -> None:
        super().__init__()

    def run(self, query: str, **kwargs: object) -> _Result:
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
        if "RETURN sr.source_record_pk AS source_record_pk" in query:
            return _Result({"source_record_pk": "sr-1"})
        return _Result()


class _Session:
    def __init__(self) -> None:
        self.tx = _Tx()

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    def execute_write(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.tx))  # type: ignore[operator]


class _Client:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def execute_read(self, callback: object) -> object:
        return callback(cast(ManagedTransaction, self.session_obj.tx))  # type: ignore[operator]

    def session(self) -> _Session:
        return self.session_obj


def _envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="sgrentalflats",
        source_record_id="rental_flat:33",
        observed_at="2026-05-08T09:47:25.177976+00:00",
        record_hash="sha256:abc",
        identifiers=[],
        attributes={
            "country_code": "SG",
            "postal_code": "681165",
            "block_no": "165A",
            "street_name": "Teck Whye Cres",
            "flat_type": "1-room & 2-room",
            "town_name": "Choa Chu Kang Town",
            "is_active": True,
        },
        raw_payload={"flat": {"id": "33"}, "town": {"id": "9"}},
    )


def test_link_source_record_to_address_uses_postal_code_parameters() -> None:
    tx = _Tx()

    link_source_record_to_address(
        cast(ManagedTransaction, tx),
        envelope=_envelope(),
        source_record_pk="sr-1",
    )

    assert len(tx.calls) == 2
    upsert_params = tx.calls[0][1]
    assert upsert_params["country_code"] == "SG"
    assert upsert_params["postal_code"] == "681165"
    assert upsert_params["street_number"] == "165A"
    assert upsert_params["street_name"] == "Teck Whye Cres"
    link_params = tx.calls[1][1]
    assert link_params["source_record_pk"] == "sr-1"
    assert link_params["source_system_key"] == "sgrentalflats"
    assert link_params["street_number"] == "165A"
    assert link_params["street_name"] == "Teck Whye Cres"
    assert link_params["unit_number"] == ""
    assert link_params["flat_type"] == "1-room & 2-room"
    assert link_params["is_active"] is True


def test_link_record_to_graph_links_every_address() -> None:
    tx = _Tx()
    envelope = SourceRecordEnvelope(
        source_system="test_source",
        source_record_id="record-1",
        observed_at="2026-05-08T09:47:25.177976+00:00",
        record_hash="sha256:def",
        identifiers=[],
        attributes={},
        raw_payload={},
    )
    addresses = [
        NormalizedAddress(
            unit_number="#05-123",
            street_number="10",
            street_name="example street",
            building_name=None,
            city="singapore",
            state_province=None,
            postal_code="123456",
            country_code="SG",
            normalized_full="#05-123, 10 example street, singapore 123456, sg",
            quality_flag=QualityFlag.VALID,
        ),
        NormalizedAddress(
            unit_number="#07-456",
            street_number="20",
            street_name="second street",
            building_name=None,
            city="singapore",
            state_province=None,
            postal_code="654321",
            country_code="SG",
            normalized_full="#07-456, 20 second street, singapore 654321, sg",
            quality_flag=QualityFlag.VALID,
        ),
    ]

    link_record_to_graph(
        cast(ManagedTransaction, tx),
        envelope=envelope,
        identifiers=[],
        addresses=addresses,
        attributes=[],
        person_id="person-1",
        source_record_pk="sr-1",
    )

    live_at_calls = [call for call in tx.calls if "MERGE (p)-[rel:LIVES_AT {" in call[0]]
    describe_calls = [
        call for call in tx.calls if "MERGE (sr)-[rel:DESCRIBES_ADDRESS]->(addr)" in call[0]
    ]
    assert len(live_at_calls) == 2
    assert len(describe_calls) == 2


def test_ingest_address_record_returns_address_result() -> None:
    client = _Client()

    result = ingest_address_record(
        cast(object, client),
        _envelope(),
        ingest_run_id="run-1",
    )

    assert isinstance(result, IngestResult)
    assert result.source_record_id == "rental_flat:33"
    assert result.ingest_run_id == "run-1"
    assert result.person_id is None
    assert result.match_decision is None
    assert result.candidate_count == 0


def test_rental_flats_is_address_only_source() -> None:
    assert _is_address_only_source("sgrentalflats") is True
    assert _is_address_only_source("sgbankruptcy") is False


class _LifecycleAddressTx(_RecordingTx):
    def __init__(self) -> None:
        super().__init__()
        self.active: dict[str, object] | None = None
        self.pending: dict[str, object] | None = None
        self.max_version = 0

    def seed_legacy_active(self, record_hash: str = "sha256:abc") -> None:
        """Model LOCK query's effective-active projection for a legacy record."""
        self.max_version = 1
        self.active = {
            "source_record_pk": "legacy-sr",
            "source_record_version": 1,
            "record_hash": record_hash,
            "lifecycle_status": "active",
            "linked_person_ids": [],
            "max_source_record_version": 1,
        }

    def run(self, query: str, **kwargs: object) -> _Result:
        self._record(query, kwargs)
        if query == queries.LOCK_AND_GET_SOURCE_STATE:
            rows = [row for row in (self.active, self.pending) if row is not None]
            if not rows:
                rows = [
                    {
                        "source_record_pk": None,
                        "source_record_version": None,
                        "record_hash": None,
                        "lifecycle_status": None,
                        "linked_person_ids": [],
                        "max_source_record_version": None,
                    }
                ]
            return _IterableResult(rows)
        if query == queries.CREATE_SOURCE_RECORD:
            self.max_version = int(str(kwargs["source_record_version"]))
            pk = f"sr-{self.max_version}"
            self.pending = {
                "source_record_pk": pk,
                "source_record_version": self.max_version,
                "record_hash": kwargs["record_hash"],
                "lifecycle_status": "pending_review",
                "linked_person_ids": [],
                "max_source_record_version": self.max_version,
            }
            return _Result({"source_record_pk": pk})
        if query in {
            queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION,
            queries.ACTIVATE_SOURCE_RECORD_VERSION,
        }:
            assert self.pending is not None
            self.pending["lifecycle_status"] = "active"
            self.active = self.pending
            self.pending = None
            return _Result({"source_record_pk": self.active["source_record_pk"]})
        return _Result()


class _IterableResult(_Result):
    def __init__(self, rows: list[dict[str, object]]) -> None:
        super().__init__(rows[0] if rows else None)
        self.rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class _LifecycleSession(_Session):
    def __init__(self, tx: _LifecycleAddressTx) -> None:
        self.tx = tx


class _LifecycleClient:
    def __init__(self) -> None:
        self.tx = _LifecycleAddressTx()

    def session(self) -> _LifecycleSession:
        return _LifecycleSession(self.tx)


def test_address_versions_lock_duplicate_replace_and_preserve_unique_version_keys() -> None:
    client = _LifecycleClient()
    first = _envelope()
    first_result = ingest_address_record(cast(object, client), first)
    duplicate_result = ingest_address_record(cast(object, client), _envelope())
    changed = _envelope().model_copy(update={"record_hash": "sha256:changed"})
    changed_result = ingest_address_record(cast(object, client), changed)

    assert first_result.source_record_pk == "sr-1"
    assert duplicate_result.skipped_duplicate is True
    assert duplicate_result.source_record_pk == "sr-1"
    assert changed_result.source_record_pk == "sr-2"
    creates = [call for call in client.tx.calls if call[0] == queries.CREATE_SOURCE_RECORD]
    assert [call[1]["source_record_version"] for call in creates] == ["1", "2"]
    assert [call[1]["source_version_key"] for call in creates] == [
        encode_source_version_key(
            "sgrentalflats",
            "rental_flat:33",
            "1",
            source_instance_id="legacy-default",
        ),
        encode_source_version_key(
            "sgrentalflats",
            "rental_flat:33",
            "2",
            source_instance_id="legacy-default",
        ),
    ]
    retires = [call for call in client.tx.calls if call[0] == queries.RETIRE_ADDRESS_PROJECTION]
    assert retires == [(queries.RETIRE_ADDRESS_PROJECTION, {"source_record_pk": "sr-1"})]
    assert any(call[0] == queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION for call in client.tx.calls)
    assert any(call[0] == queries.ACTIVATE_SOURCE_RECORD_VERSION for call in client.tx.calls)
    assert not any(call[0] == queries.MARK_PROFILE_ANALYSIS_DIRTY for call in client.tx.calls)


def test_legacy_address_duplicate_then_changed_uses_replacement_lifecycle() -> None:
    client = _LifecycleClient()
    client.tx.seed_legacy_active()

    duplicate = ingest_address_record(cast(object, client), _envelope())
    changed = _envelope().model_copy(update={"record_hash": "sha256:changed"})
    replacement = ingest_address_record(cast(object, client), changed)

    assert duplicate.skipped_duplicate is True
    assert duplicate.source_record_pk == "legacy-sr"
    assert replacement.source_record_pk == "sr-2"
    assert any(
        call == (queries.RETIRE_ADDRESS_PROJECTION, {"source_record_pk": "legacy-sr"})
        for call in client.tx.calls
    )
    activation = next(
        call for call in client.tx.calls if call[0] == queries.ACTIVATE_SOURCE_RECORD_VERSION
    )
    assert activation[1]["old_source_record_pk"] == "legacy-sr"
    assert not any(
        call[0] == queries.ACTIVATE_FIRST_SOURCE_RECORD_VERSION for call in client.tx.calls
    )
