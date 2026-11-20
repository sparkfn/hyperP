"""Unit coverage for NRIC planning, verification, and CLI behavior."""

from __future__ import annotations

import secrets

import pytest
from _nric_rehash_plan_fakes import (
    KEY_VERSION,
    FakeClient,
    _census_row,
    _graph_row,
    _plan_client,
    _report,
    _row,
    _settings,
    _valid_nric,
    _verification_client,
)
from neo4j.exceptions import DriverError, Neo4jError
from src import nric_rehash
from src.config import Settings
from src.graph.queries.nric_rehash import NRIC_IDENTIFIER_PAGE, NRIC_VERIFY_IDENTIFIERS
from src.normalizers.nric import nric_digest
from src.nric_rehash_plan import NricRehashError, build_plan_report, build_verify_report


def test_plan_pages_keyset_and_short_page_stops_with_aggregate_counts() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    digest = nric_digest(_valid_nric(), key)
    first_page = [
        _graph_row(_row("id-1", canonical, scope=None)),
        _graph_row(_row("id-2", digest, hashed_value=digest)),
    ]
    second_page = [_graph_row(_row("id-3", "unrecognized-value"))]
    client = _plan_client(
        _census_row(
            total=3,
            digest_shaped=1,
            non_digest=2,
            missing_scopes=1,
        ),
        (first_page, second_page),
    )
    report = build_plan_report(client, 2, key, KEY_VERSION)
    assert report["status"] == "blocked"
    assert report["convertible"] == 1
    assert report["already_digest"] == 1
    assert report["digest_metadata_gap"] == 1
    assert report["null_scope"] == 1
    assert report["unprocessable"]["unknown_format"] == 1
    assert report["blockers"] == ["null_scope", "unknown_format"]
    assert report["pages"] == 2
    page_parameters = [
        parameters
        for query, parameters in client.transaction.parameters
        if query == NRIC_IDENTIFIER_PAGE
    ]
    assert [parameters["after_id"] for parameters in page_parameters] == [None, "id-2"]
    assert [parameters["batch_size"] for parameters in page_parameters] == [2, 2]


def test_plan_skips_paging_when_census_has_row_blockers() -> None:
    client = _plan_client(
        _census_row(
            total=1,
            digest_shaped=0,
            non_digest=1,
            missing_ids=1,
        ),
        (),
    )
    report = build_plan_report(client, 3, secrets.token_hex(32), KEY_VERSION)
    assert report["status"] == "blocked"
    assert report["blockers"] == ["missing_id"]
    assert report["pages"] == 0


def test_plan_rejects_non_increasing_page_ids() -> None:
    canonical = _valid_nric()
    client = _plan_client(
        _census_row(total=2, digest_shaped=0, non_digest=2),
        (
            [
                _graph_row(_row("id-2", canonical)),
                _graph_row(_row("id-1", canonical)),
            ],
        ),
    )
    with pytest.raises(NricRehashError) as error:
        build_plan_report(client, 2, secrets.token_hex(32), KEY_VERSION)
    assert error.value.code == "paging_violation"


def test_verify_report_checks_all_defect_counters() -> None:
    clean_client = _verification_client(identifier_count=3)
    clean = build_verify_report(clean_client, KEY_VERSION)
    assert clean_client.transaction.parameters[0][1] == {"key_version": KEY_VERSION}
    defective = build_verify_report(
        _verification_client(
            identifier_count=3,
            non_digest_count=1,
            missing_metadata_count=2,
            duplicate_key_groups=1,
            duplicate_extra_owners=1,
            duplicate_scoped_targets=1,
        ),
        KEY_VERSION,
    )
    assert clean["ok"] is True
    assert defective["ok"] is False


def test_verify_report_rejects_each_defect_counter_individually() -> None:
    cases = (
        ("non_digest_count", _verification_client(non_digest_count=1)),
        ("missing_metadata_count", _verification_client(missing_metadata_count=1)),
        ("missing_id_count", _verification_client(missing_id_count=1)),
        ("unexpected_scope_count", _verification_client(unexpected_scope_count=1)),
        ("duplicate_key_groups", _verification_client(duplicate_key_groups=1)),
        ("duplicate_extra_owners", _verification_client(duplicate_extra_owners=1)),
        ("duplicate_scoped_targets", _verification_client(duplicate_scoped_targets=1)),
    )
    for counter_name, client in cases:
        report = build_verify_report(client, KEY_VERSION)
        assert report[counter_name] == 1
        assert report["ok"] is False


def test_verify_query_covers_null_safe_hash_id_and_scope_conditions() -> None:
    assert "hashed_value" in NRIC_VERIFY_IDENTIFIERS
    assert "AS missing_id_count" in NRIC_VERIFY_IDENTIFIERS
    assert "AS unexpected_scope_count" in NRIC_VERIFY_IDENTIFIERS
    assert "NOT coalesce(i.hashed_value = i.normalized_value, false)" in NRIC_VERIFY_IDENTIFIERS
    assert "coalesce(i.identifier_scope = 'global', false)" in NRIC_VERIFY_IDENTIFIERS
    # Real Cypher semantics are checked in the later apply PR's disposable-Neo4j shard.


def test_main_rejects_empty_key_or_version_without_constructing_client(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for key, version in (("", KEY_VERSION), (secrets.token_hex(32), "")):
        monkeypatch.setattr(
            nric_rehash,
            "get_settings",
            lambda _key=key, _version=version: _settings(_key, _version),
        )
        monkeypatch.setattr(
            nric_rehash,
            "Neo4jClient",
            lambda _settings: pytest.fail("client must not be constructed"),
        )
        assert nric_rehash.main(["verify"]) == 2
        report = _report(capsys.readouterr().out)
        assert report == {"code": "nric_key_missing", "status": "error"}


def test_settings_validation_error_does_not_leak_hmac_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    monkeypatch.setenv("NRIC_HMAC_KEY", key)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: Settings(_env_file=None))
    monkeypatch.setattr(
        nric_rehash,
        "Neo4jClient",
        lambda _settings: pytest.fail("client must not be constructed"),
    )

    assert nric_rehash.main(["verify"]) == 2
    captured = capsys.readouterr()
    assert _report(captured.out) == {"code": "invalid_configuration", "status": "error"}
    assert key not in captured.out + captured.err


@pytest.mark.parametrize("error_type", (DriverError, Neo4jError))
def test_main_sanitizes_neo4j_errors(
    error_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = secrets.token_hex(24)
    client = FakeClient({}, error=error_type(secret))
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(secrets.token_hex(32)))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)

    assert nric_rehash.main(["verify"]) == 2
    captured = capsys.readouterr()
    assert _report(captured.out) == {"code": "neo4j_error", "status": "error"}
    assert client.closed
    assert secret not in captured.out + captured.err


def test_main_plan_and_verify_output_do_not_leak_sensitive_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    digest = nric_digest(canonical, key)
    identifier_id = secrets.token_hex(12)
    plan_client = _plan_client(
        _census_row(total=1, digest_shaped=0, non_digest=1),
        (
            [
                _graph_row(_row(identifier_id, canonical)),
            ],
        ),
    )
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: plan_client)
    assert nric_rehash.main(["plan", "--batch-size", "2"]) == 0
    plan_output = capsys.readouterr()
    plan_report = _report(plan_output.out)
    assert plan_output.err == ""
    assert set(plan_report) == {
        "command",
        "status",
        "census",
        "convertible",
        "already_digest",
        "checksum_invalid_convertible",
        "null_scope",
        "digest_metadata_gap",
        "unprocessable",
        "blockers",
        "pages",
    }
    assert plan_client.closed

    verify_client = _verification_client(identifier_count=1)
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: verify_client)
    assert nric_rehash.main(["verify"]) == 0
    verify_output = capsys.readouterr()
    verify_report = _report(verify_output.out)
    assert verify_output.err == ""
    assert set(verify_report) == {
        "identifier_count",
        "non_digest_count",
        "missing_metadata_count",
        "missing_id_count",
        "unexpected_scope_count",
        "duplicate_key_groups",
        "duplicate_extra_owners",
        "duplicate_scoped_targets",
        "ok",
    }
    assert verify_client.closed
    for sensitive in (canonical, digest, key, identifier_id):
        assert sensitive not in plan_output.out + plan_output.err
        assert sensitive not in verify_output.out + verify_output.err


def test_main_maps_paging_violation_to_sanitized_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    canonical = _valid_nric()
    client = _plan_client(
        _census_row(total=2, digest_shaped=0, non_digest=2),
        (
            [
                _graph_row(_row("id-2", canonical)),
                _graph_row(_row("id-1", canonical)),
            ],
        ),
    )
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(secrets.token_hex(32)))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)

    assert nric_rehash.main(["plan"]) == 2
    assert _report(capsys.readouterr().out) == {"code": "paging_violation", "status": "error"}
    assert client.closed


def test_main_rejects_nonpositive_batch_size() -> None:
    with pytest.raises(SystemExit) as error:
        nric_rehash.main(["plan", "--batch-size", "0"])
    assert error.value.code == 2


def test_main_blocks_null_scope_census_and_keeps_scanning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    client = _plan_client(
        _census_row(total=1, digest_shaped=0, non_digest=1, missing_scopes=1),
        ([_graph_row(_row("opaque-id", canonical, scope=None))],),
    )
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)

    assert nric_rehash.main(["plan"]) == 1
    report = _report(capsys.readouterr().out)
    assert report["status"] == "blocked"
    assert report["blockers"] == ["null_scope"]
    assert report["null_scope"] == 1
    assert report["convertible"] == 1
    assert client.closed


def test_main_returns_nonzero_for_blocked_plan_and_failed_verify(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    blocked_client = _plan_client(
        _census_row(total=1, digest_shaped=0, non_digest=1),
        ([_graph_row(_row("opaque-id", "unknown-value"))],),
    )
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: blocked_client)
    assert nric_rehash.main(["plan"]) == 1
    assert _report(capsys.readouterr().out)["status"] == "blocked"

    failed_verify_client = _verification_client(non_digest_count=1)
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: failed_verify_client)
    assert nric_rehash.main(["verify"]) == 1
    assert _report(capsys.readouterr().out)["ok"] is False
