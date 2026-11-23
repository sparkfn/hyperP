"""Secure local handoff output and apply CLI path validation tests."""

from __future__ import annotations

import json
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from _nric_rehash_apply_builders import _canonical, _settings
from _nric_rehash_apply_fakes import _FakeClient
from src import nric_rehash, nric_rehash_apply
from src.graph.client import Neo4jClient
from src.normalizers.nric import nric_digest
from src.nric_rehash_handoff import (
    HandoffEntry,
    handoff_summary,
    validate_handoff_dir,
    write_handoff_file,
)
from src.nric_rehash_plan import NricRehashError


def _entry(person_id: str, reason: str) -> HandoffEntry:
    return HandoffEntry(
        person_id=person_id,
        reason=reason,
        identifier_ids=(secrets.token_hex(16),),
        source_record_pks=(secrets.token_hex(16),),
    )


def test_handoff_directory_requires_existing_private_non_symlink_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(NricRehashError) as missing_error:
        validate_handoff_dir(missing)
    assert missing_error.value.code == "handoff_dir_unsafe"

    unsafe = tmp_path / "group-accessible"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    with pytest.raises(NricRehashError) as permission_error:
        validate_handoff_dir(unsafe)
    assert permission_error.value.code == "handoff_dir_unsafe"

    secure = tmp_path / "secure"
    secure.mkdir(mode=0o700)
    alias = tmp_path / "symlink"
    alias.symlink_to(secure, target_is_directory=True)
    with pytest.raises(NricRehashError) as symlink_error:
        validate_handoff_dir(alias)
    assert symlink_error.value.code == "handoff_dir_unsafe"

    secure_parent = tmp_path / "secure-parent"
    secure_parent.mkdir(mode=0o700)
    child = secure_parent / "child"
    child.mkdir(mode=0o700)
    parent_alias = tmp_path / "parent-symlink"
    parent_alias.symlink_to(secure_parent, target_is_directory=True)
    with pytest.raises(NricRehashError) as parent_error:
        validate_handoff_dir(parent_alias / "child")
    assert parent_error.value.code == "handoff_dir_unsafe"


def test_handoff_file_is_sorted_exclusive_private_and_minimal(tmp_path: Path) -> None:
    key = secrets.token_hex(32)
    canonical = _canonical()
    digest = nric_digest(canonical, key)
    ciphertext = secrets.token_urlsafe(24)
    name = "Generated Person"
    dob = "1985-01-02"
    entries = (
        HandoffEntry(
            person_id="person-b",
            reason="missing_source_context",
            identifier_ids=("identifier-z", "identifier-a"),
            source_record_pks=("record-b", "record-a"),
        ),
        HandoffEntry(
            person_id="person-a",
            reason="checksum_invalid",
            identifier_ids=("identifier-c",),
            source_record_pks=("record-c",),
        ),
    )

    basename = write_handoff_file(tmp_path, entries)

    assert basename is not None
    output = tmp_path / basename
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    contents = output.read_bytes()
    rows = [json.loads(line) for line in contents.decode("utf-8").splitlines()]
    assert [row["person_id"] for row in rows] == ["person-a", "person-b"]
    assert set(rows[0]) == {"person_id", "reason", "identifier_ids", "source_record_pks"}
    assert rows[1]["identifier_ids"] == ["identifier-a", "identifier-z"]
    assert rows[1]["source_record_pks"] == ["record-a", "record-b"]
    assert handoff_summary(entries, basename) == {
        "total": 2,
        "by_reason": {"checksum_invalid": 1, "missing_source_context": 1},
        "file": basename,
    }
    for secret in (key, canonical, digest, ciphertext, name, dob):
        assert secret.encode("utf-8") not in contents


def test_handoff_writer_refuses_existing_names_and_symlink_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = tmp_path / "opaque.jsonl"
    existing.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr("src.nric_rehash_handoff._handoff_basename", lambda: existing.name)

    with pytest.raises(NricRehashError) as collision:
        write_handoff_file(tmp_path, (_entry("person", "multi_nric"),))
    assert collision.value.code == "handoff_write_failed"
    assert existing.read_text(encoding="utf-8") == "preserve"

    target = tmp_path / "target.jsonl"
    target.write_text("preserve", encoding="utf-8")
    link = tmp_path / "linked.jsonl"
    link.symlink_to(target)
    monkeypatch.setattr("src.nric_rehash_handoff._handoff_basename", lambda: link.name)
    with pytest.raises(NricRehashError) as symlink:
        write_handoff_file(tmp_path, (_entry("person", "multi_nric"),))
    assert symlink.value.code == "handoff_write_failed"
    assert target.read_text(encoding="utf-8") == "preserve"


def test_apply_validates_directory_before_reading_the_database(tmp_path: Path) -> None:
    key = secrets.token_hex(32)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    client = _FakeClient()

    with pytest.raises(NricRehashError) as error:
        nric_rehash_apply.run_nric_rehash_apply(
            cast(Neo4jClient, client),
            batch_size=10,
            key=key,
            key_version="generated-version",
            expected_database="neo4j",
            acknowledge_offline=True,
            handoff_dir=unsafe,
        )

    assert error.value.code == "handoff_dir_unsafe"
    assert client.query_parameters == []
    assert client.write_calls == 0


def test_cli_requires_handoff_dir_and_rejects_unsafe_path_before_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as missing:
        nric_rehash.main(["apply", "--acknowledge-offline", "--expect-database", "neo4j"])
    assert missing.value.code == 2

    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    monkeypatch.setattr(
        nric_rehash,
        "get_settings",
        lambda: pytest.fail("unsafe handoff directory must be rejected first"),
    )
    monkeypatch.setattr(
        nric_rehash,
        "Neo4jClient",
        lambda _settings: pytest.fail("unsafe handoff directory must be rejected first"),
    )
    assert (
        nric_rehash.main(
            [
                "apply",
                "--acknowledge-offline",
                "--expect-database",
                "neo4j",
                "--handoff-dir",
                str(unsafe),
            ]
        )
        == 2
    )
    output: object = json.loads(capsys.readouterr().out)
    assert output == {"code": "handoff_dir_unsafe", "status": "error"}


def test_handoff_write_failure_is_exit_one_with_sanitized_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    key = secrets.token_hex(32)
    client = _FakeClient()
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(key))
    monkeypatch.setattr(nric_rehash, "Neo4jClient", lambda _settings: client)

    def fail_write(_directory: Path, _entries: Sequence[HandoffEntry]) -> str | None:
        raise NricRehashError("handoff_write_failed")

    monkeypatch.setattr(nric_rehash_apply, "write_handoff_file", fail_write)
    assert (
        nric_rehash.main(
            [
                "apply",
                "--acknowledge-offline",
                "--expect-database",
                "neo4j",
                "--handoff-dir",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out) == {
        "code": "handoff_write_failed",
        "status": "error",
    }
    assert client.closed


def test_cli_acknowledgement_precedes_unsafe_directory_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    unsafe.chmod(0o755)
    monkeypatch.setattr(nric_rehash, "get_settings", lambda: _settings(secrets.token_hex(32)))
    monkeypatch.setattr(
        nric_rehash,
        "Neo4jClient",
        lambda _settings: pytest.fail("acknowledgement must be checked before client creation"),
    )

    assert (
        nric_rehash.main(["apply", "--expect-database", "neo4j", "--handoff-dir", str(unsafe)]) == 2
    )
    output: object = json.loads(capsys.readouterr().out)
    assert output == {"code": "offline_ack_required", "status": "error"}
