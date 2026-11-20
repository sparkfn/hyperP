"""Legacy NRIC replay mapping preserves historical assertion identities."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Iterator, Sequence
from typing import TypeVar, cast

import pytest
from _nric_rehash_apply_builders import _KEY_VERSION, _canonical, _settings
from _nric_rehash_neo4j_fingerprint import _safe_equal
from _nric_rehash_neo4j_seed import _invalid_checksum, _valid_canonical
from _nric_rehash_neo4j_seed_replay import _historical_assertion_id
from neo4j import ManagedTransaction
from src import identity_assertion_nric, identity_assertions
from src.config import Settings
from src.graph.client import Neo4jClient
from src.graph.queries.identity_assertions import (
    ASSERT_IDENTIFIERS,
    MERGE_ASSERTION_IDENTIFIERS_BATCH,
)
from src.identity_assertion_nric import LegacyNricReplayError, keyed_identifier_rows
from src.models import JsonValue
from src.normalizers.nric import mask_nric, nric_digest

_TestT = TypeVar("_TestT")


def _identifier_entry(value: str, *, identifier_type: str = "nric") -> dict[str, JsonValue]:
    return {
        "identifier_type": identifier_type,
        "normalized_value": value,
        "source_instance_id": None,
        "quality_flag": "valid",
    }


def _legacy_payload(value: str) -> dict[str, JsonValue]:
    return {"identifiers": [_identifier_entry(value)]}


class _RecordingTransaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> object:
        self.calls.append((query, parameters))
        return object()


class _Rows:
    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self.rows = tuple(rows)

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self.rows)


class _PagedTransaction:
    def __init__(self, rows: Sequence[dict[str, object]]) -> None:
        self.rows = rows

    def run(self, _query: str, **_parameters: object) -> _Rows:
        return _Rows(self.rows)


class _PagedClient:
    def __init__(self, pages: Sequence[Sequence[dict[str, object]]]) -> None:
        self.pages = tuple(tuple(page) for page in pages)
        self.read_index = 0
        self.write_calls: list[_RecordingTransaction] = []
        self.closed = False

    def execute_read(self, work: Callable[[ManagedTransaction], _TestT]) -> _TestT:
        page = self.pages[self.read_index]
        self.read_index += 1
        return work(cast(ManagedTransaction, _PagedTransaction(page)))

    def execute_write(self, work: Callable[[ManagedTransaction], _TestT]) -> _TestT:
        tx = _RecordingTransaction()
        self.write_calls.append(tx)
        return work(cast(ManagedTransaction, tx))

    def close(self) -> None:
        self.closed = True


def test_write_assertions_keys_legacy_row_and_keeps_historical_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    key = secrets.token_hex(32)
    source_record_pk = secrets.token_hex(16)
    payload = _legacy_payload(canonical)
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: _settings(key))
    tx = _RecordingTransaction()

    identity_assertions.write_assertions(cast(ManagedTransaction, tx), source_record_pk, payload)

    assert [query for query, _ in tx.calls] == [
        MERGE_ASSERTION_IDENTIFIERS_BATCH,
        ASSERT_IDENTIFIERS,
    ]
    merged = tx.calls[0][1]["identifiers"]
    asserted = tx.calls[1][1]["assertions"]
    assert isinstance(merged, list) and isinstance(asserted, list)
    merge_row = cast(dict[str, JsonValue], merged[0])
    assertion_row = cast(dict[str, JsonValue], asserted[0])
    digest = nric_digest(canonical, key)
    _safe_equal(merge_row["normalized_value"], digest, "merged NRIC target")
    _safe_equal(merge_row["hashed_value"], digest, "merged hashed target")
    _safe_equal(assertion_row["normalized_value"], digest, "assertion NRIC target")
    _safe_equal(assertion_row["hashed_value"], digest, "assertion hashed target")
    _safe_equal(
        assertion_row["assertion_id"],
        _historical_assertion_id(source_record_pk, canonical),
        "historical assertion identity",
    )
    assert "target_id" not in assertion_row
    if canonical in json.dumps(tx.calls, sort_keys=True, default=str):
        pytest.fail("plaintext NRIC reached an identifier statement")


def test_variants_share_keyed_target_but_keep_distinct_historical_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    key = secrets.token_hex(32)
    source_pks = (secrets.token_hex(16), secrets.token_hex(16))
    stored_values = (canonical.lower(), f"x{canonical}")
    rows = [
        identity_assertions.assertion_rows(pk, _legacy_payload(raw)).identifiers[0]
        for pk, raw in zip(source_pks, stored_values, strict=True)
    ]
    original_rows = [dict(row) for row in rows]
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: _settings(key))

    keyed = keyed_identifier_rows(rows)

    digest = nric_digest(canonical, key)
    _safe_equal(
        tuple(row["normalized_value"] for row in keyed),
        (digest, digest),
        "variant digest targets",
    )
    _safe_equal(
        tuple(row["hashed_value"] for row in keyed),
        (digest, digest),
        "variant hashed targets",
    )
    expected_ids = tuple(
        _historical_assertion_id(pk, raw) for pk, raw in zip(source_pks, stored_values, strict=True)
    )
    _safe_equal(
        tuple(row["assertion_id"] for row in keyed),
        expected_ids,
        "variant historical assertion ids",
    )
    if keyed[0]["assertion_id"] == keyed[1]["assertion_id"]:
        pytest.fail("normalized variants lost distinct historical assertion ids")
    _safe_equal(rows, original_rows, "input rows changed")


def test_checksum_invalid_recoverable_value_is_still_keyed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    invalid = _invalid_checksum(canonical)
    key = secrets.token_hex(32)
    row = identity_assertions.assertion_rows("generated-record", _legacy_payload(invalid))
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: _settings(key))

    mapped = keyed_identifier_rows(row.identifiers)

    digest = nric_digest(invalid, key)
    _safe_equal(mapped[0]["normalized_value"], digest, "invalid-checksum NRIC target")
    _safe_equal(mapped[0]["hashed_value"], digest, "invalid-checksum hash target")


def test_digest_and_non_nric_rows_do_not_load_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = secrets.token_hex(32)
    digest = nric_digest(_canonical(), key)
    digest_row = identity_assertions.assertion_rows(
        "generated-record", _legacy_payload(digest)
    ).identifiers[0]
    phone_row = identity_assertions.assertion_rows(
        "generated-record",
        {"identifiers": [_identifier_entry("+6591234567", identifier_type="phone")]},
    ).identifiers[0]
    monkeypatch.setattr(
        identity_assertion_nric,
        "get_settings",
        lambda: pytest.fail("already-digested or non-NRIC rows need no key"),
    )

    mapped = keyed_identifier_rows([digest_row, phone_row])

    _safe_equal(mapped, [digest_row, phone_row], "digest and non-NRIC row pass-through")
    assert mapped[0] is not digest_row
    assert mapped[1] is not phone_row


def test_empty_key_fails_before_any_identifier_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    key = secrets.token_hex(32)
    payload = _legacy_payload(canonical)
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: _settings(""))
    tx = _RecordingTransaction()

    with pytest.raises(LegacyNricReplayError) as error:
        identity_assertions.write_assertions(
            cast(ManagedTransaction, tx), "generated-record", payload
        )

    assert str(error.value) == "legacy NRIC assertion replay refused"
    for sensitive in (canonical, key):
        if sensitive in str(error.value):
            pytest.fail("replay error included a sensitive generated value")
    assert tx.calls == []


def test_unprocessable_values_pass_through_and_blockers_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    masked = mask_nric(canonical)
    unprocessable = [
        identity_assertions.assertion_rows("generated-record", _legacy_payload(masked)).identifiers[
            0
        ],
        identity_assertions.assertion_rows(
            "generated-record", _legacy_payload("generated-unknown-format")
        ).identifiers[0],
    ]
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: _settings(""))

    _safe_equal(keyed_identifier_rows(unprocessable), unprocessable, "unprocessable rows changed")

    tenant_row = identity_assertions.assertion_rows(
        "generated-record", _legacy_payload(canonical)
    ).identifiers[0]
    tenant_row["identifier_scope"] = "tenant-private"
    missing_id = dict(tenant_row)
    missing_id["identifier_scope"] = "global"
    missing_id.pop("assertion_id", None)
    for row in (tenant_row, missing_id):
        with pytest.raises(LegacyNricReplayError) as error:
            keyed_identifier_rows([row])
        assert str(error.value) == "legacy NRIC assertion replay refused"


def test_keying_is_idempotent_and_does_not_mutate_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    key = secrets.token_hex(32)
    rows = identity_assertions.assertion_rows(
        "generated-record", _legacy_payload(canonical)
    ).identifiers
    original = [dict(row) for row in rows]
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: _settings(key))

    keyed = keyed_identifier_rows(rows)

    _safe_equal(rows, original, "input rows changed")
    monkeypatch.setattr(
        identity_assertion_nric,
        "get_settings",
        lambda: pytest.fail("digest replay must not load settings"),
    )
    _safe_equal(keyed_identifier_rows(keyed), keyed, "keyed replay was not idempotent")


def test_assertion_rows_and_audit_remain_key_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _valid_canonical(set())
    payload = _legacy_payload(canonical)
    source_record_pk = secrets.token_hex(16)
    monkeypatch.setattr(
        identity_assertion_nric,
        "get_settings",
        lambda: pytest.fail("row building and audit do not need a key"),
    )
    rows = identity_assertions.assertion_rows(source_record_pk, payload)
    _safe_equal(
        rows.identifiers[0]["target_id"],
        f"nric|global|{canonical}",
        "legacy assertion target id",
    )
    _safe_equal(
        rows.identifiers[0]["assertion_id"],
        _historical_assertion_id(source_record_pk, canonical),
        "legacy assertion identity",
    )
    client = _PagedClient(
        [
            [
                {
                    "source_record_pk": source_record_pk,
                    "normalized_payload": json.dumps(payload),
                    "fallback_identifiers": [],
                    "identifier_count": 1,
                    "address_count": 0,
                    "fact_count": 0,
                }
            ],
            [],
        ]
    )
    assert identity_assertions.audit_assertions(cast(Neo4jClient, client), batch_size=1) == []


def test_main_backfill_fails_closed_with_one_fixed_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    canonical = _valid_canonical(set())
    payload = _legacy_payload(canonical)
    source_record_pk = secrets.token_hex(16)
    record = {
        "source_record_pk": source_record_pk,
        "normalized_payload": json.dumps(payload),
        "fallback_identifiers": [],
    }
    client = _PagedClient([[record], []])
    settings = Settings.model_construct(
        identity_model="v2_shadow",
        nric_hmac_key="",
        nric_hmac_key_version=_KEY_VERSION,
        neo4j_password="",
    )
    monkeypatch.setattr(identity_assertions, "get_settings", lambda: settings)
    monkeypatch.setattr(identity_assertion_nric, "get_settings", lambda: settings)
    monkeypatch.setattr(identity_assertions, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr("sys.argv", ["identity_assertions", "backfill"])

    assert identity_assertions.main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "NRIC assertion replay refused\n"
    assert client.closed
    assert client.write_calls[0].calls == []
