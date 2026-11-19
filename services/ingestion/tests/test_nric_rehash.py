"""Unit coverage for the read-only NRIC rehash planner and verifier."""

from __future__ import annotations

import json
import secrets
import string
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from neo4j.exceptions import DriverError, Neo4jError
from src import nric_rehash
from src.config import Settings
from src.graph.queries import (
    NRIC_IDENTIFIER_CENSUS,
    NRIC_IDENTIFIER_PAGE,
    NRIC_VERIFY_DUPLICATE_PERSON_KEYS,
    NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS,
    NRIC_VERIFY_IDENTIFIERS,
)
from src.normalizers.nric import (
    is_nric_digest,
    mask_nric,
    nric_checksum_valid,
    nric_digest,
)
from src.nric_rehash_classify import (
    NricClassification,
    NricIdentifierRow,
    classify_nric_identifier,
)
from src.nric_rehash_plan import (
    NricRehashError,
    build_plan_report,
    build_verify_report,
)

KEY_VERSION = "test-key-version"
_TestT = TypeVar("_TestT")


class FakeRecord:
    def __init__(self, values: Mapping[str, object]) -> None:
        self.values = dict(values)

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


class FakeResult:
    def __init__(self, rows: Sequence[Record]) -> None:
        self.rows = tuple(rows)

    def __iter__(self) -> Iterator[Record]:
        return iter(self.rows)

    def single(self) -> Record | None:
        return self.rows[0] if self.rows else None


class FakeTransaction:
    def __init__(
        self,
        responses: Mapping[str, Sequence[Sequence[Mapping[str, object]]]],
    ) -> None:
        self.responses = {query: list(calls) for query, calls in responses.items()}
        self.parameters: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> FakeResult:
        self.parameters.append((query, parameters))
        queued = self.responses.get(query)
        if not queued:
            raise AssertionError("unexpected query execution")
        rows = queued.pop(0)
        return FakeResult(tuple(cast(Record, FakeRecord(row)) for row in rows))


class FakeClient:
    def __init__(
        self,
        responses: Mapping[str, Sequence[Sequence[Mapping[str, object]]]],
        error: BaseException | None = None,
    ) -> None:
        self.transaction = FakeTransaction(responses)
        self.error = error
        self.closed = False

    def execute_read(self, work: Callable[[ManagedTransaction], _TestT]) -> _TestT:
        if self.error is not None:
            raise self.error
        return work(cast(ManagedTransaction, self.transaction))

    def close(self) -> None:
        self.closed = True


def _valid_nric() -> str:
    while True:
        digits = f"{secrets.randbelow(10_000_000):07d}"
        if len(set(digits)) == 1:
            continue
        for letter in string.ascii_uppercase:
            candidate = f"S{digits}{letter}"
            if nric_checksum_valid(candidate):
                return candidate


def _invalid_checksum(canonical: str) -> str:
    for letter in string.ascii_uppercase:
        candidate = f"{canonical[:-1]}{letter}"
        if candidate != canonical and not nric_checksum_valid(candidate):
            return candidate
    raise AssertionError("could not synthesize an invalid checksum")


def _row(
    identifier_id: str | None,
    value: object,
    *,
    scope: str | None = "global",
    hashed_value: str | None = None,
    nric_masked: str | None = None,
    checksum_valid: bool | None = None,
    key_version: str | None = None,
    encrypted: str | None = None,
) -> NricIdentifierRow:
    return NricIdentifierRow(
        identifier_id=identifier_id,
        identifier_scope=scope,
        normalized_value=value if isinstance(value, str) else None,
        hashed_value=hashed_value,
        nric_masked=nric_masked,
        checksum_valid=checksum_valid,
        nric_key_version=key_version,
        nric_encrypted=encrypted,
    )


def _complete_digest_row(identifier_id: str, digest: str) -> NricIdentifierRow:
    return _row(
        identifier_id,
        digest,
        hashed_value=digest,
        nric_masked=mask_nric(_valid_nric()),
        checksum_valid=True,
        key_version=KEY_VERSION,
        encrypted=secrets.token_urlsafe(24),
    )


def _graph_row(row: NricIdentifierRow) -> dict[str, object]:
    return {
        "identifier_id": row.identifier_id,
        "identifier_scope": row.identifier_scope,
        "normalized_value": row.normalized_value,
        "hashed_value": row.hashed_value,
        "nric_masked": row.nric_masked,
        "checksum_valid": row.checksum_valid,
        "nric_key_version": row.nric_key_version,
        "nric_encrypted": row.nric_encrypted,
    }


def _census_row(
    *,
    total: int,
    digest_shaped: int,
    non_digest: int,
    missing_ids: int = 0,
    missing_scopes: int = 0,
    unexpected_scopes: int = 0,
) -> dict[str, object]:
    return {
        "total": total,
        "digest_shaped": digest_shaped,
        "non_digest": non_digest,
        "missing_ids": missing_ids,
        "missing_scopes": missing_scopes,
        "unexpected_scopes": unexpected_scopes,
    }


def _plan_client(
    census: Mapping[str, object], pages: Sequence[Sequence[Mapping[str, object]]]
) -> FakeClient:
    return FakeClient(
        {
            NRIC_IDENTIFIER_CENSUS: [[census]],
            NRIC_IDENTIFIER_PAGE: pages,
        }
    )


def _verification_client(
    *,
    identifier_count: int = 0,
    non_digest_count: int = 0,
    missing_metadata_count: int = 0,
    duplicate_key_groups: int = 0,
    duplicate_extra_owners: int = 0,
    duplicate_scoped_targets: int = 0,
) -> FakeClient:
    return FakeClient(
        {
            NRIC_VERIFY_IDENTIFIERS: [
                [
                    {
                        "identifier_count": identifier_count,
                        "non_digest_count": non_digest_count,
                        "missing_metadata_count": missing_metadata_count,
                    }
                ]
            ],
            NRIC_VERIFY_DUPLICATE_PERSON_KEYS: [
                [
                    {
                        "duplicate_key_groups": duplicate_key_groups,
                        "duplicate_extra_owners": duplicate_extra_owners,
                    }
                ]
            ],
            NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS: [
                [{"duplicate_scoped_targets": duplicate_scoped_targets}]
            ],
        }
    )


def _settings(key: str, key_version: str = KEY_VERSION) -> Settings:
    return Settings.model_construct(
        nric_hmac_key=key,
        nric_hmac_key_version=key_version,
        neo4j_password="",
    )


def _report(output: str) -> dict[str, object]:
    decoded: object = json.loads(output)
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def test_classification_converts_canonicalized_variants_to_same_digest() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    variants = (canonical, f"xxx{canonical}", canonical.lower())
    classifications = [
        classify_nric_identifier(_row(f"id-{index}", value), key, KEY_VERSION)
        for index, value in enumerate(variants)
    ]
    assert all(result.category == "convertible" for result in classifications)
    assert {result.digest for result in classifications} == {nric_digest(canonical, key)}
    assert all(result.canonical == canonical for result in classifications)
    assert all(result.masked == mask_nric(canonical) for result in classifications)


def test_checksum_invalid_shape_is_still_convertible() -> None:
    classification = classify_nric_identifier(
        _row("opaque-id", _invalid_checksum(_valid_nric())), secrets.token_hex(32), KEY_VERSION
    )
    assert classification.category == "convertible"
    assert classification.checksum_valid is False
    assert classification.digest is not None


def test_sensitive_values_are_excluded_from_row_and_classification_repr() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    digest = nric_digest(canonical, key)
    masked = mask_nric(canonical)
    ciphertext = secrets.token_urlsafe(24)
    row = NricIdentifierRow(
        identifier_id="opaque-id",
        identifier_scope="global",
        normalized_value=canonical,
        hashed_value=digest,
        nric_masked=masked,
        checksum_valid=nric_checksum_valid(canonical),
        nric_key_version=KEY_VERSION,
        nric_encrypted=ciphertext,
    )
    classification = classify_nric_identifier(row, key, KEY_VERSION)
    representation = f"{row!r}{classification!r}"

    assert isinstance(classification, NricClassification)
    assert classification.category == "convertible"
    for sensitive in (canonical, digest, masked, ciphertext):
        assert sensitive not in representation


def test_digest_metadata_gaps_include_missing_mismatch_and_wrong_version() -> None:
    digest = nric_digest(_valid_nric(), secrets.token_hex(32))
    complete = _complete_digest_row("opaque-complete", digest)
    missing = _row("opaque-missing", digest, hashed_value=digest)
    wrong_version = _row(
        "opaque-version",
        digest,
        hashed_value=digest,
        nric_masked=mask_nric(_valid_nric()),
        checksum_valid=True,
        key_version="older-version",
        encrypted=secrets.token_urlsafe(24),
    )
    mismatch = _row(
        "opaque-hash-mismatch",
        digest,
        hashed_value=nric_digest(_valid_nric(), secrets.token_hex(32)),
        nric_masked=mask_nric(_valid_nric()),
        checksum_valid=True,
        key_version=KEY_VERSION,
        encrypted=secrets.token_urlsafe(24),
    )
    results = [
        classify_nric_identifier(row, secrets.token_hex(32), KEY_VERSION)
        for row in (complete, missing, wrong_version, mismatch)
    ]
    assert all(result.category == "already_digest" for result in results)
    assert [result.digest_metadata_gap for result in results] == [False, True, True, True]


@pytest.mark.parametrize(
    ("value_factory", "expected_reason"),
    [
        (lambda value: mask_nric(value), "masked"),
        (lambda _value: None, "empty"),
        (lambda _value: 123, "empty"),
        (lambda _value: "xxxx", "empty"),
        (lambda _value: "not-an-identifier", "unknown_format"),
    ],
)
def test_unprocessable_classification_reasons(
    value_factory: Callable[[str], object], expected_reason: str
) -> None:
    row = _row("opaque-id", value_factory(_valid_nric()))
    classified = classify_nric_identifier(row, secrets.token_hex(32), KEY_VERSION)
    assert classified.category == "unprocessable"
    assert classified.reason == expected_reason


def test_digest_lookalikes_are_not_accepted_as_digests() -> None:
    digest = nric_digest(_valid_nric(), secrets.token_hex(32))
    uppercase_hex = f"A{digest[1:]}"
    assert is_nric_digest(digest)
    lookalikes = (uppercase_hex, digest[:-1], f"{digest}a")
    for index, value in enumerate(lookalikes):
        classified = classify_nric_identifier(
            _row(f"opaque-{index}", value), secrets.token_hex(32), KEY_VERSION
        )
        assert classified.category == "unprocessable"
        assert classified.reason == "unknown_format"


def test_missing_id_unexpected_scope_and_null_scope() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    missing_id = classify_nric_identifier(_row(None, canonical), key, KEY_VERSION)
    unexpected_scope = classify_nric_identifier(
        _row("opaque-tenant", canonical, scope="tenant-private"), key, KEY_VERSION
    )
    null_scope = classify_nric_identifier(
        _row("opaque-null-scope", canonical, scope=None), key, KEY_VERSION
    )
    assert missing_id.blocker == "missing_id"
    assert unexpected_scope.blocker == "unexpected_scope"
    assert null_scope.category == "convertible"


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
