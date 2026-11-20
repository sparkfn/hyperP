"""Unit coverage for completed markers, preconditions, and CLI setup."""

from __future__ import annotations

import secrets
from typing import cast

import pytest
from _nric_rehash_apply_builders import _KEY_VERSION, _canonical, _report, _row, _settings
from _nric_rehash_apply_fakes import _FakeClient
from src import nric_rehash, nric_rehash_apply
from src.graph.client import Neo4jClient
from src.nric_rehash_plan import NricRehashError
from src.nric_rehash_state import key_check_for


def _complete_state(
    key: str, revision: int | None, *, key_version: str = _KEY_VERSION
) -> dict[str, object]:
    return {
        "phase": "convert_complete",
        "cursor": None,
        "key_version": key_version,
        "key_check": key_check_for(key),
        "baseline_total": 0,
        "baseline_non_digest": 0,
        "baseline_digest_shaped": 0,
        "baseline_asserts": 0,
        "baseline_identified_by": 0,
        "baseline_anchored_by": 0,
        "baseline_moved": 0,
        "baseline_has_identifier": 0,
        "baseline_active_links": 0,
        "baseline_revision": revision,
        "batches": 4,
        "converted_in_place": 0,
        "converted_consolidated": 0,
        "removed_nodes": 0,
    }


def test_completed_marker_has_zero_writes_and_no_encryption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = secrets.token_hex(32)
    client = _FakeClient(state=_complete_state(key, 29))
    monkeypatch.setattr(
        nric_rehash_apply,
        "encrypt_nric",
        lambda _canonical, _key: pytest.fail("completed rerun must not encrypt"),
    )
    report, exit_code = nric_rehash_apply.run_nric_rehash_apply(
        cast(Neo4jClient, client),
        batch_size=2,
        key=key,
        key_version=_KEY_VERSION,
        expected_database="neo4j",
        acknowledge_offline=True,
    )
    assert exit_code == 0
    assert report["changes"] == 0
    assert report["status"] == "ok"
    assert client.write_calls == 0


def test_a2a_marker_without_evidence_baseline_fails_closed() -> None:
    key = secrets.token_hex(32)
    state = _complete_state(key, 29)
    del state["baseline_asserts"]
    client = _FakeClient(state=state)
    with pytest.raises(NricRehashError) as error:
        nric_rehash_apply.run_nric_rehash_apply(
            cast(Neo4jClient, client),
            batch_size=10,
            key=key,
            key_version=_KEY_VERSION,
            expected_database="neo4j",
            acknowledge_offline=True,
        )
    assert error.value.code == "drift_detected"
    assert client.write_calls == 0


def test_preconditions_and_changed_key_fail_before_any_write() -> None:
    key = secrets.token_hex(32)
    cases = (
        (_row(None, _canonical()), "missing_id"),
        (_row("opaque-id", _canonical(), "tenant-private"), "unexpected_scope"),
        (_row("opaque-id", _canonical(), None), "null_scope_present"),
    )
    for row, expected_code in cases:
        client = _FakeClient([row])
        with pytest.raises(NricRehashError) as error:
            nric_rehash_apply.run_nric_rehash_apply(
                cast(Neo4jClient, client),
                batch_size=10,
                key=key,
                key_version=_KEY_VERSION,
                expected_database="neo4j",
                acknowledge_offline=True,
            )
        assert error.value.code == expected_code
        assert client.write_calls == 0

    changed = _FakeClient(state=_complete_state(secrets.token_hex(32), 29))
    with pytest.raises(NricRehashError) as error:
        nric_rehash_apply.run_nric_rehash_apply(
            cast(Neo4jClient, changed),
            batch_size=10,
            key=key,
            key_version=_KEY_VERSION,
            expected_database="neo4j",
            acknowledge_offline=True,
        )
    assert error.value.code == "key_changed"
    assert changed.write_calls == 0


def test_missing_hmac_key_is_sanitized_before_client_construction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(""))
    monkeypatch.setattr(
        nric_rehash,
        "Neo4jClient",
        lambda _settings: pytest.fail("missing key must not construct a client"),
    )
    assert nric_rehash.main(["apply", "--acknowledge-offline", "--expect-database", "neo4j"]) == 2
    assert _report(capsys.readouterr().out) == {"code": "nric_key_missing", "status": "error"}


def test_database_and_acknowledgement_preconditions_have_exit_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(
        nric_rehash,
        "Neo4jClient",
        lambda _settings: pytest.fail("missing acknowledgement must not connect"),
    )
    assert nric_rehash.main(["apply", "--expect-database", "neo4j"]) == 2
    assert _report(capsys.readouterr().out) == {
        "code": "offline_ack_required",
        "status": "error",
    }

    client = _FakeClient(database_name="different-db")
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)
    assert nric_rehash.main(["apply", "--acknowledge-offline", "--expect-database", "neo4j"]) == 2
    assert _report(capsys.readouterr().out) == {
        "code": "database_mismatch",
        "status": "error",
    }
    assert client.write_calls == 0
    assert client.closed


def test_apply_requires_expected_database_argument() -> None:
    with pytest.raises(SystemExit) as error:
        nric_rehash.main(["apply", "--acknowledge-offline"])
    assert error.value.code == 2
