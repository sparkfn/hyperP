from __future__ import annotations

from typing import cast

from neo4j import ManagedTransaction
from src.graph import queries
from src.main import _is_address_only_source
from src.models import IngestResult, NormalizedAddress, QualityFlag, SourceRecordEnvelope
from src.pipeline_addresses import ingest_address_record
from src.pipeline_writes import link_record_to_graph, link_source_record_to_address


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


class _Tx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
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

    live_at_calls = [call for call in tx.calls if "MERGE (p)-[rel:LIVES_AT]->(addr)" in call[0]]
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
