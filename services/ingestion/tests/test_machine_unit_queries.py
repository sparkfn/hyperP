from __future__ import annotations

from typing import cast

import pytest
from neo4j import ManagedTransaction
from src.exclusion_config import ExclusionFile
from src.exclusions import ExclusionContext, build_exclusion_context
from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import RecordType, SourceRecordEnvelope
from src.pipeline import IngestPipeline
from src.pipeline_sales import _write_machine_unit_observations


def test_machine_unit_queries_are_exported() -> None:
    assert "MachineUnit" in queries.UPSERT_MACHINE_UNIT
    assert "INVOLVES_UNIT" in queries.LINK_ORDER_INVOLVES_UNIT
    assert "BOUGHT_UNIT" in queries.LINK_PERSON_BOUGHT_UNIT
    assert "OWNS_UNIT" in queries.LINK_PERSON_OWNS_UNIT
    assert "conflict_flag" in queries.FLAG_MACHINE_UNIT_OWNER_CONFLICTS


def test_machine_unit_upsert_scopes_lta_and_serial_matches_by_product() -> None:
    query = queries.UPSERT_MACHINE_UNIT

    assert "normalized_machine_product: $normalized_machine_product" in query
    assert "normalized_lta_tag: $normalized_lta_tag" in query
    assert "normalized_serial_number: $normalized_serial_number" in query
    assert "machine_product: $machine_product" in query
    assert "machine_unit_identifier_conflict" in query


def test_machine_unit_matched_branch_excludes_identifier_conflicts() -> None:
    query = queries.UPSERT_MACHINE_UNIT

    assert "WITH lta_unit, serial_unit, coalesce(lta_unit, serial_unit) AS matched" in query
    assert (
        "WHERE matched IS NOT NULL AND ("
        "lta_unit IS NULL OR serial_unit IS NULL OR lta_unit = serial_unit)" in query
    )


def test_machine_unit_conflict_branch_preserves_product_fields() -> None:
    query = queries.UPSERT_MACHINE_UNIT

    assert (
        "lta_unit.machine_product = coalesce(lta_unit.machine_product, $machine_product)" in query
    )
    assert (
        "serial_unit.machine_product = coalesce(serial_unit.machine_product, $machine_product)"
        in query
    )
    assert (
        "lta_unit.normalized_machine_product = coalesce("
        "lta_unit.normalized_machine_product, $normalized_machine_product)" in query
    )
    assert (
        "serial_unit.normalized_machine_product = coalesce("
        "serial_unit.normalized_machine_product, $normalized_machine_product)" in query
    )


def test_chat_machine_unit_query_does_not_create_units() -> None:
    query = queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT

    assert "CREATE (" not in query
    assert "MERGE (u:MachineUnit" not in query
    assert "MATCH (u:MachineUnit" in query
    assert "normalized_lta_tag" in query
    assert "normalized_serial_number" in query


def test_chat_mentions_unit_query_links_existing_source_record_to_unit() -> None:
    query = queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT

    assert "MATCH (sr:SourceRecord" in query
    assert "MATCH (u:MachineUnit" in query
    assert "MERGE (sr)-[rel:MENTIONS_UNIT]->(u)" in query
    assert "CREATE (u:MachineUnit" not in query


class _Result:
    def __init__(self, row: dict[str, object] | None = None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _SalesMachineUnitTx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query == queries.UPSERT_MACHINE_UNIT:
            return _Result({"machine_unit_id": "unit-1"})
        return _Result()


class _ChatMachineUnitTx:
    def __init__(self, machine_unit_ids: list[str]) -> None:
        self._machine_unit_ids = machine_unit_ids
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **kwargs: object) -> _Result:
        self.calls.append((query, kwargs))
        if query == queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT:
            return _Result({"machine_unit_ids": self._machine_unit_ids})
        return _Result()


def _machine_unit_exclusion_context() -> ExclusionContext:
    return build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(
            machine_unit_identifiers=[
                {"machine_product": "Servicing Labour", "serial_number": "1186#1506"}
            ]
        ),
    )


def _chat_machine_unit_envelope() -> SourceRecordEnvelope:
    return SourceRecordEnvelope(
        source_system="eko",
        source_record_id="chat-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-05-18T10:00:00+00:00",
        record_hash="sha256:chat-1",
        raw_payload={
            "inquiries": [
                {
                    "machine_product": "Bike A",
                    "lta_tag": "LTA123",
                    "serial_number": None,
                    "notes": "mentioned unit",
                }
            ]
        },
        extraction_confidence=0.82,
        extraction_method="test_fixture",
        conversation_ref={"thread_id": "thread-1"},
    )


def _write_chat_machine_unit_observations(machine_unit_ids: list[str]) -> _ChatMachineUnitTx:
    tx = _ChatMachineUnitTx(machine_unit_ids)
    pipeline = IngestPipeline(cast(Neo4jClient, object()))

    pipeline._write_chat_machine_unit_observations(  # noqa: SLF001
        cast(ManagedTransaction, tx),
        envelope=_chat_machine_unit_envelope(),
        source_record_pk="sr-1",
        exclusion_context=build_exclusion_context(
            company_mobile_numbers=[],
            company_email_addresses=[],
            internal_person_names=[],
            file_exclusions=ExclusionFile(),
        ),
    )

    return tx


def _recorded_queries(tx: _ChatMachineUnitTx) -> list[str]:
    return [query for query, _params in tx.calls]


def test_chat_machine_unit_observation_links_exactly_one_existing_unit() -> None:
    tx = _write_chat_machine_unit_observations(["unit-1"])

    assert _recorded_queries(tx) == [
        queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT,
        queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT,
    ]
    link_params = tx.calls[1][1]
    assert link_params["source_record_pk"] == "sr-1"
    assert link_params["machine_unit_id"] == "unit-1"
    assert link_params["source_system_key"] == "eko"
    assert link_params["source_record_id"] == "chat-1"
    assert link_params["raw_context"] == "mentioned unit"
    assert queries.UPSERT_MACHINE_UNIT not in _recorded_queries(tx)


def test_sales_machine_unit_observation_skips_excluded_product_serial_pair() -> None:
    tx = _SalesMachineUnitTx()

    _write_machine_unit_observations(
        cast(ManagedTransaction, tx),
        source_system_key="speedzone_phppos:sales",
        source_record_pk="sr-1",
        source_record_id="sale-1",
        source_order_id="order-1",
        observed_at="2026-05-18T10:00:00+00:00",
        line_items=[
            {
                "source_line_id": "line-1",
                "serial_number": "1186#1506",
                "product": {"display_name": "Servicing Labour"},
            }
        ],
        person_id="person-1",
        exclusion_context=_machine_unit_exclusion_context(),
    )

    assert tx.calls == []


def test_chat_machine_unit_observation_skips_excluded_product_serial_pair() -> None:
    tx = _ChatMachineUnitTx(["unit-1"])
    pipeline = IngestPipeline(cast(Neo4jClient, object()))
    envelope = SourceRecordEnvelope(
        source_system="eko",
        source_record_id="chat-1",
        record_type=RecordType.CONVERSATION,
        observed_at="2026-05-18T10:00:00+00:00",
        record_hash="sha256:chat-1",
        raw_payload={
            "inquiries": [
                {
                    "machine_product": "Servicing Labour",
                    "serial_number": "1186#1506",
                    "notes": "noisy unit",
                }
            ]
        },
        extraction_confidence=0.82,
        extraction_method="test_fixture",
        conversation_ref={"thread_id": "thread-1"},
    )

    pipeline._write_chat_machine_unit_observations(  # noqa: SLF001
        cast(ManagedTransaction, tx),
        envelope=envelope,
        source_record_pk="sr-1",
        exclusion_context=_machine_unit_exclusion_context(),
    )

    assert tx.calls == []


@pytest.mark.parametrize("machine_unit_ids", [[], ["unit-1", "unit-2"]])
def test_chat_machine_unit_observation_skips_link_without_exactly_one_match(
    machine_unit_ids: list[str],
) -> None:
    tx = _write_chat_machine_unit_observations(machine_unit_ids)

    assert _recorded_queries(tx) == [queries.RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT]
    assert queries.LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT not in _recorded_queries(tx)
    assert queries.UPSERT_MACHINE_UNIT not in _recorded_queries(tx)
