"""Unit coverage for resumable NRIC apply orchestration using a fake client."""

from __future__ import annotations

import json
import secrets
import string
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from neo4j.exceptions import DriverError
from src import nric_rehash, nric_rehash_apply
from src.config import Settings
from src.graph import queries
from src.graph.client import Neo4jClient
from src.graph.queries import (
    NRIC_IDENTIFIER_CENSUS,
    NRIC_IDENTIFIER_PAGE,
    NRIC_VERIFY_DUPLICATE_PERSON_KEYS,
    NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS,
    NRIC_VERIFY_IDENTIFIERS,
)
from src.normalizers.nric import is_nric_digest, mask_nric, nric_checksum_valid, nric_digest
from src.nric_rehash_plan import NricRehashError
from src.nric_rehash_state import key_check_for

_T = TypeVar("_T")
_KEY_VERSION = "apply-test-version"


class _Result:
    def __init__(self, rows: Sequence[Record]) -> None:
        self.rows = tuple(rows)

    def __iter__(self) -> Iterator[Record]:
        return iter(self.rows)

    def single(self) -> Record | None:
        return self.rows[0] if self.rows else None


def _result(*rows: Mapping[str, object]) -> _Result:
    return _Result(tuple(cast(Record, dict(row)) for row in rows))


class _FakeTx:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client

    def run(self, query: str, **parameters: object) -> _Result:
        self.client.query_parameters.append((query, parameters))
        if query == NRIC_IDENTIFIER_CENSUS:
            return _result(self._census())
        if query == NRIC_IDENTIFIER_PAGE:
            return self._page(parameters.get("after_id"), parameters.get("batch_size"))
        if query == queries.NRIC_REHASH_DATABASE_NAME:
            return _result({"name": self.client.database_name})
        if query == queries.NRIC_REHASH_READ_STATE:
            return self._read_state()
        if query == queries.NRIC_REHASH_READ_REVISION:
            return _result({"current_revision": self.client.revision})
        if query == NRIC_VERIFY_IDENTIFIERS:
            return _result(self._verify_identifiers(parameters.get("key_version")))
        if query == NRIC_VERIFY_DUPLICATE_PERSON_KEYS:
            return _result({"duplicate_key_groups": 0, "duplicate_extra_owners": 0})
        if query == NRIC_VERIFY_DUPLICATE_SCOPED_TARGETS:
            return _result({"duplicate_scoped_targets": self._duplicate_targets()})
        if query == queries.NRIC_REHASH_ACQUIRE_LEASE:
            return self._acquire(parameters)
        if query == queries.NRIC_REHASH_FENCE_BATCH:
            return self._fence(parameters)
        if query == queries.NRIC_REHASH_PAGE_AND_LOCK_IDENTIFIERS:
            return self._page(parameters.get("cursor"), parameters.get("batch_size"))
        if query == queries.NRIC_REHASH_FIND_DIGEST_TARGET:
            return self._find_digest(parameters.get("digest"))
        if query == queries.NRIC_REHASH_UPDATE_IDENTIFIER:
            return self._update_identifier(parameters)
        if query == queries.NRIC_REHASH_ADVANCE_BATCH:
            return self._advance(parameters)
        if query == queries.NRIC_REHASH_COMPLETE_AND_RELEASE:
            return self._complete(parameters)
        if query == queries.NRIC_REHASH_RELEASE_LEASE:
            if self.client.release_error is not None:
                raise self.client.release_error
            return self._release(parameters)
        raise AssertionError("unexpected Cypher statement")

    def _census(self) -> dict[str, object]:
        rows = self.client.identifiers
        values = [row.get("normalized_value") for row in rows]
        digest_flags = [isinstance(value, str) and is_nric_digest(value) for value in values]
        scopes = [row.get("identifier_scope") for row in rows]
        return {
            "total": len(rows),
            "digest_shaped": sum(digest_flags),
            "non_digest": len(rows) - sum(digest_flags),
            "missing_ids": sum(row.get("identifier_id") is None for row in rows),
            "missing_scopes": sum(scope is None for scope in scopes),
            "unexpected_scopes": sum(scope is not None and scope != "global" for scope in scopes),
        }

    def _page(self, cursor: object, batch_size: object) -> _Result:
        if not isinstance(batch_size, int):
            raise AssertionError("page size was not an integer")
        rows = sorted(
            self.client.identifiers,
            key=lambda row: str(row.get("identifier_id") or ""),
        )
        after = cursor if isinstance(cursor, str) else None
        selected = [
            row for row in rows if after is None or str(row.get("identifier_id") or "") > after
        ][:batch_size]
        return _result(
            *[
                {
                    **row,
                    "element_id": f"test-element-{row.get('identifier_id')}",
                }
                for row in selected
            ]
        )

    def _read_state(self) -> _Result:
        state = self.client.state
        if state is None:
            return _result({"exists": False})
        return _result({"exists": True, **state})

    def _verify_identifiers(self, key_version: object) -> dict[str, object]:
        rows = self.client.identifiers
        values = [row.get("normalized_value") for row in rows]
        metadata_gaps = 0
        for row, value in zip(rows, values, strict=True):
            if not isinstance(value, str) or not is_nric_digest(value):
                continue
            if (
                any(
                    row.get(key) is None
                    for key in (
                        "nric_masked",
                        "checksum_valid",
                        "nric_key_version",
                        "nric_encrypted",
                    )
                )
                or row.get("nric_key_version") != key_version
            ):
                metadata_gaps += 1
        digest_flags = [isinstance(value, str) and is_nric_digest(value) for value in values]
        non_digest = len(rows) - sum(digest_flags)
        return {
            "identifier_count": len(rows),
            "non_digest_count": non_digest,
            "missing_metadata_count": metadata_gaps,
        }

    def _duplicate_targets(self) -> int:
        values = [
            row.get("normalized_value")
            for row in self.client.identifiers
            if row.get("identifier_scope") in (None, "global")
        ]
        return len({value for value in values if values.count(value) > 1})

    def _acquire(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None:
            state = {
                "phase": "convert",
                "cursor": None,
                "key_version": parameters["key_version"],
                "key_check": parameters["key_check"],
                "baseline_total": parameters["baseline_total"],
                "baseline_non_digest": parameters["baseline_non_digest"],
                "baseline_digest_shaped": parameters["baseline_digest_shaped"],
                "baseline_revision": parameters["baseline_revision"],
                "batches": 0,
                "converted_in_place": 0,
                "converted_consolidated": 0,
                "removed_nodes": 0,
                "lock_version": 0,
            }
            self.client.state = state
        state["lock_version"] = int(state.get("lock_version", 0)) + 1
        owner = parameters["owner"]
        if state.get("lease_owner") not in (None, owner):
            return _result()
        if (
            state.get("key_version") != parameters["key_version"]
            or state.get("key_check") != parameters["key_check"]
        ):
            return _result(
                {
                    "acquired": True,
                    "key_matches": False,
                    **state,
                }
            )
        if parameters.get("restart_completed") is True and state.get("phase") == "convert_complete":
            state["phase"] = "convert"
            state["cursor"] = None
        state["lease_owner"] = owner
        state["offline_acknowledged_at"] = state.get("offline_acknowledged_at", "ack")
        return _result({"acquired": True, "key_matches": True, **state})

    def _fence(self, parameters: Mapping[str, object]) -> _Result:
        if self.client.state is None or self.client.state.get("lease_owner") != parameters.get(
            "owner"
        ):
            return _result()
        self.client.state["lock_version"] = int(self.client.state.get("lock_version", 0)) + 1
        return _result({"fenced": True})

    def _find_digest(self, digest: object) -> _Result:
        matches = [
            {"identifier_id": row.get("identifier_id")}
            for row in self.client.identifiers
            if row.get("identifier_type") == "nric"
            and row.get("identifier_scope") == "global"
            and row.get("normalized_value") == digest
        ]
        return _result(*matches[:1])

    def _update_identifier(self, parameters: Mapping[str, object]) -> _Result:
        element_id = parameters.get("element_id")
        digest = parameters.get("digest")
        for row in self.client.identifiers:
            row_id = row.get("identifier_id")
            row_element_id = f"test-element-{row_id}" if isinstance(row_id, str) else None
            if (
                row_element_id == element_id
                and row.get("identifier_type") == "nric"
                and row.get("identifier_scope") == "global"
                and not is_nric_digest(str(row.get("normalized_value") or ""))
                and not any(
                    other is not row
                    and other.get("identifier_type") == "nric"
                    and other.get("identifier_scope") == "global"
                    and other.get("normalized_value") == digest
                    for other in self.client.identifiers
                )
            ):
                row.update(
                    normalized_value=digest,
                    hashed_value=parameters.get("hashed_value"),
                    nric_key_version=parameters.get("key_version"),
                    nric_masked=parameters.get("masked"),
                    checksum_valid=parameters.get("checksum_valid"),
                    nric_encrypted=parameters.get("ciphertext"),
                )
                return _result({"updated": 1})
        return _result({"updated": 0})

    def _advance(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None or state.get("lease_owner") != parameters.get("owner"):
            return _result()
        state["cursor"] = parameters["cursor"]
        state["batches"] = int(state["batches"]) + 1
        state["converted_in_place"] = int(state["converted_in_place"]) + int(
            parameters["converted_in_place"]
        )
        return _result(
            {
                "batches": state["batches"],
                "converted_in_place": state["converted_in_place"],
            }
        )

    def _complete(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None or state.get("lease_owner") != parameters.get("owner"):
            return _result()
        state["phase"] = "convert_complete"
        state["cursor"] = None
        state.pop("lease_owner", None)
        return _result({"completed": True})

    def _release(self, parameters: Mapping[str, object]) -> _Result:
        state = self.client.state
        if state is None or state.get("lease_owner") != parameters.get("owner"):
            return _result()
        state.pop("lease_owner", None)
        return _result({"released": True})


class _FakeClient:
    def __init__(
        self,
        identifiers: Sequence[Mapping[str, object]] = (),
        *,
        database_name: str = "neo4j",
        revision: int | None = 29,
        state: dict[str, object] | None = None,
        release_error: BaseException | None = None,
    ) -> None:
        self.identifiers = [dict(row) for row in identifiers]
        self.database_name = database_name
        self.revision = revision
        self.state = deepcopy(state)
        self.release_error = release_error
        self.write_calls = 0
        self.query_parameters: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def execute_read(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        return work(cast(ManagedTransaction, _FakeTx(self)))

    def execute_write(self, work: Callable[[ManagedTransaction], _T]) -> _T:
        self.write_calls += 1
        identifiers = deepcopy(self.identifiers)
        state = deepcopy(self.state)
        try:
            return work(cast(ManagedTransaction, _FakeTx(self)))
        except BaseException:
            self.identifiers = identifiers
            self.state = state
            raise

    def close(self) -> None:
        self.closed = True


def _canonical(exclude: set[str] | None = None) -> str:
    excluded = exclude or set()
    while True:
        digits = f"{secrets.randbelow(10_000_000):07d}"
        if len(set(digits)) == 1:
            continue
        for letter in string.ascii_uppercase:
            candidate = f"S{digits}{letter}"
            if candidate not in excluded and nric_checksum_valid(candidate):
                return candidate


def _row(
    identifier_id: str | None, value: object, scope: str | None = "global"
) -> dict[str, object]:
    return {
        "identifier_id": identifier_id,
        "identifier_type": "nric",
        "identifier_scope": scope,
        "normalized_value": value,
        "hashed_value": None,
        "nric_masked": None,
        "checksum_valid": None,
        "nric_key_version": None,
        "nric_encrypted": None,
    }


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
        "baseline_revision": revision,
        "batches": 4,
        "converted_in_place": 0,
        "converted_consolidated": 0,
        "removed_nodes": 0,
    }


def _settings(key: str, key_version: str = _KEY_VERSION) -> Settings:
    return Settings.model_construct(
        nric_hmac_key=key,
        nric_hmac_key_version=key_version,
        neo4j_password="",
    )


def _report(output: str) -> dict[str, object]:
    decoded: object = json.loads(output)
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)


def _assert_not_leaked(output: str, sensitive: str, label: str) -> None:
    if sensitive in output:
        pytest.fail(f"{label} leaked to command output")


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


def test_collision_unsupported_has_exit_one_and_sanitized_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    canonical = _canonical()
    digest = nric_digest(canonical, key)
    cipher = secrets.token_urlsafe(24)
    plain_id = secrets.token_hex(12)
    digest_id = secrets.token_hex(12)
    plaintext = _row(f"a-{plain_id}", canonical)
    existing_digest = _row(f"b-{digest_id}", digest)
    existing_digest.update(
        hashed_value=digest,
        nric_masked=mask_nric(canonical),
        checksum_valid=True,
        nric_key_version=_KEY_VERSION,
        nric_encrypted=cipher,
    )
    client = _FakeClient([plaintext, existing_digest])
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)

    assert nric_rehash.main(["apply", "--acknowledge-offline", "--expect-database", "neo4j"]) == 1
    captured = capsys.readouterr()
    assert _report(captured.out) == {"code": "collision_unsupported", "status": "error"}
    assert client.closed
    for label, sensitive in (
        ("NRIC", canonical),
        ("digest", digest),
        ("key", key),
        ("ciphertext", cipher),
        ("Identifier id", plain_id),
        ("Identifier id", digest_id),
    ):
        _assert_not_leaked(captured.out + captured.err, sensitive, label)


def test_release_driver_error_does_not_replace_original_apply_error() -> None:
    key = secrets.token_hex(32)
    canonical = _canonical()
    digest = nric_digest(canonical, key)
    plaintext = _row("a-plain", canonical)
    existing_digest = _row("b-digest", digest)
    existing_digest.update(
        hashed_value=digest,
        nric_masked=mask_nric(canonical),
        checksum_valid=True,
        nric_key_version=_KEY_VERSION,
        nric_encrypted=secrets.token_urlsafe(24),
    )
    client = _FakeClient(
        [plaintext, existing_digest],
        release_error=DriverError(secrets.token_hex(24)),
    )

    with pytest.raises(NricRehashError) as error:
        nric_rehash_apply.run_nric_rehash_apply(
            cast(Neo4jClient, client),
            batch_size=10,
            key=key,
            key_version=_KEY_VERSION,
            expected_database="neo4j",
            acknowledge_offline=True,
        )
    assert error.value.code == "collision_unsupported"


def test_apply_progress_and_final_json_never_leak_sensitive_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = secrets.token_hex(32)
    canonical = _canonical()
    identifier_id = secrets.token_hex(12)
    element_id = f"test-element-{identifier_id}"
    cursor = identifier_id
    owner = secrets.token_hex(16)
    digest = nric_digest(canonical, key)
    masked = mask_nric(canonical)
    ciphertext = secrets.token_urlsafe(24)
    encrypted_values: list[str] = []
    client = _FakeClient([_row(identifier_id, canonical)])

    def encrypt(value: str, _key: str) -> str:
        encrypted_values.append(value)
        return ciphertext

    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(nric_rehash_apply, "encrypt_nric", encrypt)
    monkeypatch.setattr(nric_rehash_apply.uuid, "uuid4", lambda: uuid.UUID(hex=owner))

    assert (
        nric_rehash.main(
            ["apply", "--batch-size", "1", "--acknowledge-offline", "--expect-database", "neo4j"]
        )
        == 0
    )
    captured = capsys.readouterr()
    report = _report(captured.out)
    progress = [json.loads(line) for line in captured.err.splitlines()]
    assert report["status"] == "ok"
    assert report["changes"] == 1
    assert report["converted_in_place"] == 1
    assert report["batches"] == 1
    assert progress == [{"batches": 1, "converted_in_place": 1, "event": "batch"}]
    assert set(progress[0]) == {"event", "batches", "converted_in_place"}
    if encrypted_values != [canonical]:
        pytest.fail("encryptor received an unexpected number of rows")
    update_query = queries.NRIC_REHASH_UPDATE_IDENTIFIER
    if "elementId(i) = $element_id" not in update_query or "$identifier_id" in update_query:
        pytest.fail("update query did not use an internal element id lookup")
    update_parameters = [
        parameters for query, parameters in client.query_parameters if query == update_query
    ]
    if len(update_parameters) != 1 or canonical in update_parameters[0].values():
        pytest.fail("plaintext was passed as a Cypher parameter")
    if any(
        canonical in parameters.values() or key in parameters.values()
        for _, parameters in client.query_parameters
    ):
        pytest.fail("plaintext or HMAC key was passed as a Cypher parameter")
    assert client.closed
    for label, sensitive in (
        ("NRIC", canonical),
        ("digest", digest),
        ("HMAC key", key),
        ("Identifier id", identifier_id),
        ("element id", element_id),
        ("cursor", cursor),
        ("lease owner", owner),
        ("masked NRIC", masked),
        ("ciphertext", ciphertext),
    ):
        _assert_not_leaked(captured.out + captured.err, sensitive, label)
