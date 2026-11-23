"""Unit coverage for apply outcomes, sanitized output, and progress."""

from __future__ import annotations

import json
import secrets
import uuid
from typing import cast

import pytest
from _nric_rehash_apply_builders import (
    _KEY_VERSION,
    _canonical,
    _report,
    _row,
    _settings,
)
from _nric_rehash_apply_fakes import _FakeClient
from neo4j.exceptions import DriverError
from src import nric_rehash, nric_rehash_apply, nric_rehash_passes
from src.graph.client import Neo4jClient
from src.graph.queries.nric_rehash import NRIC_REHASH_UPDATE_IDENTIFIER
from src.normalizers.nric import mask_nric, nric_digest
from src.nric_rehash_plan import NricRehashError


def _assert_not_leaked(output: str, sensitive: str, label: str) -> None:
    if sensitive in output:
        pytest.fail(f"{label} leaked to command output")


def test_unsupported_edge_has_exit_one_and_sanitized_output(
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
    client = _FakeClient(
        [plaintext, existing_digest],
        relationship_inventory=(
            {
                "relationship_type": "UNSUPPORTED_RELATION",
                "loser_is_start": False,
                "other_labels": ["SourceRecord"],
                "relationship_count": 1,
            },
        ),
    )
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)

    assert nric_rehash.main(["apply", "--acknowledge-offline", "--expect-database", "neo4j"]) == 1
    captured = capsys.readouterr()
    assert _report(captured.out) == {"code": "unsupported_edge", "status": "error"}
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
        relationship_inventory=(
            {
                "relationship_type": "UNSUPPORTED_RELATION",
                "loser_is_start": False,
                "other_labels": ["SourceRecord"],
                "relationship_count": 1,
            },
        ),
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
    assert error.value.code == "unsupported_edge"


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
    monkeypatch.setattr(nric_rehash_passes, "encrypt_nric", encrypt)
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
    assert progress == [
        {
            "batches": 1,
            "converted_in_place": 1,
            "converted_consolidated": 0,
            "removed_nodes": 0,
            "event": "batch",
        }
    ]
    assert set(progress[0]) == {
        "event",
        "batches",
        "converted_in_place",
        "converted_consolidated",
        "removed_nodes",
    }
    if encrypted_values != [canonical]:
        pytest.fail("encryptor received an unexpected number of rows")
    update_query = NRIC_REHASH_UPDATE_IDENTIFIER
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
