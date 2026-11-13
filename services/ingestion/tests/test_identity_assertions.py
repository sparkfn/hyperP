"""Pure-row, write, and fallback-backfill tests for identity assertions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction
from src.graph.client import Neo4jClient
from src.graph.queries.identity_assertions import ASSERT_IDENTIFIERS
from src.identity_assertions import (
    _parse_normalized_payload,
    assertion_rows,
    audit_assertions,
    backfill_assertions,
    write_assertions,
)
from src.models import JsonValue

T = TypeVar("T")


def _payload() -> dict[str, JsonValue]:
    return {
        "identifiers": [
            {
                "identifier_type": "phone",
                "normalized_value": "+6591234567",
                "source_instance_id": None,
                "quality_flag": "valid",
            },
            {
                "identifier_type": "email",
                "normalized_value": "invalid@example.com",
                "source_instance_id": None,
                "quality_flag": "invalid_format",
            },
            {
                "identifier_type": "email",
                "normalized_value": "partial@example.com",
                "source_instance_id": None,
                "quality_flag": "partial_parse",
            },
            {
                "identifier_type": "phone",
                "normalized_value": "+6591234567",
                "source_instance_id": None,
                "quality_flag": "valid",
            },
        ],
        "addresses": [
            {
                "country_code": "SG",
                "postal_code": "123456",
                "street_name": "Example Road",
                "street_number": "10",
                "unit_number": None,
                "quality_flag": "valid",
            },
            {
                "country_code": "SG",
                "postal_code": "654321",
                "street_name": "Bad Road",
                "street_number": "2",
                "unit_number": None,
                "quality_flag": "placeholder_value",
            },
        ],
        "attributes": [
            {"attribute_name": "full_name", "attribute_value": "Ada Example"},
            {"attribute_name": "dob", "attribute_value": "1990-01-02"},
            {"attribute_name": "email", "attribute_value": "ignored@example.com"},
        ],
    }


class _RecordingTransaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> object:
        self.calls.append((query, parameters))
        return object()


def test_assertion_rows_are_deterministic_usable_and_deduplicated() -> None:
    payload = _payload()
    first = assertion_rows("source-1", payload)
    second = assertion_rows("source-1", payload)

    assert first == second
    assert {row["identifier_type"] for row in first.identifiers} == {"email", "phone"}
    assert len(first.addresses) == 1
    assert {row["attribute_name"] for row in first.facts} == {"full_name", "dob"}


def test_assertion_id_matches_documented_sha1_formula() -> None:
    rows = assertion_rows("source-1", _payload()).identifiers
    row = next(item for item in rows if item["normalized_value"] == "partial@example.com")
    target_id = "email|global|partial@example.com"
    identity = f"source-1|identifiers.email|{target_id}"

    assert row["target_id"] == target_id
    assert row["assertion_id"] == hashlib.sha1(identity.encode("utf-8")).hexdigest()


def test_nric_identifier_row_populates_metadata_and_non_nric_sets_none() -> None:
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"
    payload: dict[str, JsonValue] = {
        "identifiers": [
            {
                "identifier_type": "phone",
                "normalized_value": "+6591234567",
                "quality_flag": "valid",
            },
            {
                "identifier_type": "nric",
                "normalized_value": digest,
                "quality_flag": "valid",
                "nric_masked": "S••••567A",
                "checksum_valid": False,
                "nric_key_version": "v1",
            },
        ]
    }
    rows = {row["identifier_type"]: row for row in assertion_rows("source-1", payload).identifiers}

    phone = rows["phone"]
    assert phone["hashed_value"] is None
    assert phone["nric_masked"] is None
    assert phone["checksum_valid"] is None
    assert phone["nric_key_version"] is None
    assert phone["nric_encrypted"] is None

    nric = rows["nric"]
    assert nric["hashed_value"] == nric["normalized_value"]
    assert nric["nric_masked"] == "S••••567A"
    assert nric["checksum_valid"] is False
    assert nric["nric_key_version"] == "v1"
    assert nric["nric_encrypted"] is None


def test_json_roundtrip_preserves_live_assertion_rows() -> None:
    payload = _payload()
    parsed = _parse_normalized_payload(json.dumps(payload))

    assert parsed is not None
    assert assertion_rows("source-1", parsed) == assertion_rows("source-1", payload)


def test_write_assertions_runs_only_needed_upserts_and_immutable_assertions() -> None:
    tx = _RecordingTransaction()
    write_assertions(cast(ManagedTransaction, tx), "source-1", _payload())

    assert len(tx.calls) == 6
    assertion_queries = [query for query, _ in tx.calls if "ASSERTS" in query]
    assert len(assertion_queries) == 3
    assert all("ON MATCH SET" not in query for query in assertion_queries)
    assert all("ON CREATE SET" in query for query in assertion_queries)
    assert any(query == ASSERT_IDENTIFIERS for query, _ in tx.calls)


class _QueryResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)


class _PageTransaction:
    def __init__(self, rows: list[dict[str, object]], calls: list[str]) -> None:
        self._rows = rows
        self._calls = calls

    def run(self, query: str, **_parameters: object) -> _QueryResult:
        self._calls.append(query)
        return _QueryResult(self._rows)


class _FakeClient:
    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self._pages = pages
        self._read_index = 0
        self.read_calls: list[str] = []
        self.write_calls: list[str] = []

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        rows = self._pages[self._read_index]
        self._read_index += 1
        return work(cast(ManagedTransaction, _PageTransaction(rows, self.read_calls)))

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, _PageTransaction([], self.write_calls)))


def test_backfill_falls_back_to_identified_by_edges_when_payload_is_invalid() -> None:
    fallback_identifiers: list[JsonValue] = [
        {
            "identifier_type": "phone",
            "identifier_scope": "global",
            "normalized_value": "+6591234567",
            "source_instance_id": None,
            "quality_flag": "valid",
        }
    ]
    client = _FakeClient(
        [
            [
                {
                    "source_record_pk": "source-1",
                    "normalized_payload": "{not-json",
                    "fallback_identifiers": fallback_identifiers,
                }
            ],
            [],
        ]
    )

    assert backfill_assertions(cast(Neo4jClient, client), batch_size=1) == 1
    assert any("IDENTIFIED_BY" in query for query in client.read_calls)
    assert ASSERT_IDENTIFIERS in client.write_calls


def test_unusable_quality_flag_entry_is_not_malformed() -> None:
    payload: dict[str, JsonValue] = {
        "identifiers": [
            {
                "identifier_type": "phone",
                "normalized_value": "+6591234567",
                "quality_flag": "invalid_format",
            }
        ]
    }
    rows = assertion_rows("source-1", payload)
    assert rows.malformed == 0
    assert len(rows.identifiers) == 0


def test_legacy_shaped_payload_records_malformed_and_audit_reports_it() -> None:
    legacy_payload: dict[str, JsonValue] = {
        "identifiers": [
            {
                "type": "phone",
                "value": "+6591234567",
            }
        ]
    }
    rows = assertion_rows("source-1", legacy_payload)
    assert rows.malformed == 1
    assert len(rows.identifiers) == 0

    client = _FakeClient(
        [
            [
                {
                    "source_record_pk": "source-legacy",
                    "normalized_payload": json.dumps(legacy_payload),
                    "fallback_identifiers": [],
                    "identifier_count": 0,
                    "address_count": 0,
                    "fact_count": 0,
                }
            ],
            [],
        ]
    )
    mismatches = audit_assertions(cast(Neo4jClient, client), batch_size=1)
    assert len(mismatches) == 1
    assert mismatches[0].source_record_pk == "source-legacy"
    assert mismatches[0].malformed_entries == 1


def test_present_non_list_identifiers_container_is_counted_as_malformed() -> None:
    # The audit routes this shape to the IDENTIFIED_BY fallback instead (edges stay the authority).
    assert assertion_rows("source-1", {"identifiers": {"unexpected": "object"}}).malformed == 1


@pytest.mark.parametrize("key", ["addresses", "attributes"])
def test_present_non_list_container_is_malformed_and_audit_reports_it(key: str) -> None:
    payload: dict[str, JsonValue] = {
        "identifiers": [],
        "addresses": [],
        "attributes": [],
        key: {"unexpected": "object"},
    }
    rows = assertion_rows("source-1", payload)
    assert rows.malformed == 1
    assert not (rows.identifiers or rows.addresses or rows.facts)

    client = _FakeClient(
        [
            [
                {
                    "source_record_pk": "source-bad-container",
                    "normalized_payload": json.dumps(payload),
                    "fallback_identifiers": [],
                    "identifier_count": 0,
                    "address_count": 0,
                    "fact_count": 0,
                }
            ],
            [],
        ]
    )
    mismatches = audit_assertions(cast(Neo4jClient, client), batch_size=1)
    assert [m.malformed_entries for m in mismatches] == [1]


def test_absent_containers_are_not_malformed() -> None:
    assert assertion_rows("source-1", {}).malformed == 0
    assert assertion_rows("source-1", {"attributes": None, "addresses": None}).malformed == 0


def test_empty_addresses_list_falls_back_to_the_legacy_address() -> None:
    legacy: dict[str, JsonValue] = {
        "country_code": "SG",
        "postal_code": "123456",
        "street_name": "Example Road",
        "street_number": "10",
        "quality_flag": "valid",
    }
    for addresses in ([], None):
        payload: dict[str, JsonValue] = {"address": legacy}
        if addresses is not None:
            payload["addresses"] = addresses
        rows = assertion_rows("source-1", payload)
        assert [row["postal_code"] for row in rows.addresses] == ["123456"]
        assert rows.malformed == 0
    # A populated list wins over the legacy field.
    other: dict[str, JsonValue] = {**legacy, "postal_code": "654321"}
    rows = assertion_rows("source-1", {"address": legacy, "addresses": [other]})
    assert [row["postal_code"] for row in rows.addresses] == ["654321"]


def test_payload_without_identifiers_list_uses_fallback_for_backfill_and_audit() -> None:
    fallback_identifiers: list[JsonValue] = [
        {
            "identifier_type": "phone",
            "identifier_scope": "global",
            "normalized_value": "+6591234567",
            "source_instance_id": None,
            "quality_flag": "valid",
        }
    ]
    payload_no_id_list = json.dumps({"attributes": []})

    audit_client = _FakeClient(
        [
            [
                {
                    "source_record_pk": "source-1",
                    "normalized_payload": payload_no_id_list,
                    "fallback_identifiers": fallback_identifiers,
                    "identifier_count": 1,
                    "address_count": 0,
                    "fact_count": 0,
                }
            ],
            [],
        ]
    )
    mismatches = audit_assertions(cast(Neo4jClient, audit_client), batch_size=1)
    assert mismatches == []

    backfill_client = _FakeClient(
        [
            [
                {
                    "source_record_pk": "source-1",
                    "normalized_payload": payload_no_id_list,
                    "fallback_identifiers": fallback_identifiers,
                }
            ],
            [],
        ]
    )
    assert backfill_assertions(cast(Neo4jClient, backfill_client), batch_size=1) == 1
    assert ASSERT_IDENTIFIERS in backfill_client.write_calls


def test_empty_object_payload_with_no_identified_by_edges_has_no_mismatch() -> None:
    client = _FakeClient(
        [
            [
                {
                    "source_record_pk": "source-empty",
                    "normalized_payload": "{}",
                    "fallback_identifiers": [],
                    "identifier_count": 0,
                    "address_count": 0,
                    "fact_count": 0,
                }
            ],
            [],
        ]
    )
    mismatches = audit_assertions(cast(Neo4jClient, client), batch_size=1)
    assert mismatches == []
