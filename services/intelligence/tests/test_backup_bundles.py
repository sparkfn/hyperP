"""Verified immutable backup-bundle acceptance tests."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json, sha256_file
from intelligence.config import RuntimeConfig
from intelligence.registry import Cancelled, RegisteredCommand, Registry
from intelligence.runtime import IntelligenceRuntime
from intelligence.state import State


def bundle_success_handler(directory: Path, cancelled: Cancelled) -> None:
    """Produce one small reviewed artifact for a real completed-run backup."""
    del cancelled
    (directory / "result.json").write_text('{"result":"ok"}', encoding="utf-8")


def _completed_run(tmp_path: Path) -> tuple[State, str]:
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True),
        Registry((RegisteredCommand("approved", True, bundle_success_handler, {}),)),
    )
    try:
        run_id = runtime.run("approved")
    finally:
        runtime.close()
    return State(tmp_path), run_id


def _bundle(tmp_path: Path) -> tuple[State, Path, str]:
    state, run_id = _completed_run(tmp_path)
    bundle = state.layout.backups / "accepted.bundle"
    state.backup(bundle)
    return state, bundle, run_id


def test_bundle_contains_snapshot_manifest_and_accepted_evidence(tmp_path: Path) -> None:
    """A bundle captures durable SQLite plus every completed-run manifest and output."""
    state, bundle, run_id = _bundle(tmp_path)
    try:
        state.verify_backup(bundle)
        inventory = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        evidence_paths = {entry["path"] for entry in inventory["evidence"]}
        assert inventory["state_snapshot"]["path"] == "state.sqlite3"
        assert f"evidence/manifests/{run_id}.json" in evidence_paths
        assert f"evidence/outputs/{run_id}/result.json" in evidence_paths
        assert (bundle / "evidence" / "outputs" / run_id / "result.json").is_file()
        with pytest.raises(FileExistsError):
            state.backup(bundle)
    finally:
        state.close()


def test_bundle_survives_state_reopen(tmp_path: Path) -> None:
    """Backup verification does not depend on the creating SQLite connection."""
    state, bundle, _ = _bundle(tmp_path)
    state.close()
    reopened = State(tmp_path)
    try:
        reopened.verify_backup(bundle)
    finally:
        reopened.close()


@pytest.mark.parametrize("target", ("state.sqlite3", "evidence/outputs"))
def test_bundle_rejects_tampered_snapshot_or_evidence(tmp_path: Path, target: str) -> None:
    """Checksums reject a modified snapshot as well as copied accepted evidence."""
    state, bundle, run_id = _bundle(tmp_path)
    try:
        path = bundle / target
        if path.is_dir():
            path = path / run_id / "result.json"
        with path.open("ab") as handle:
            handle.write(b"tamper")
        with pytest.raises(ValueError):
            state.verify_backup(bundle)
    finally:
        state.close()


def test_bundle_rejects_missing_evidence_and_corrupt_manifest(tmp_path: Path) -> None:
    """Missing copied evidence and noncanonical metadata are never accepted as backups."""
    state, bundle, run_id = _bundle(tmp_path)
    try:
        (bundle / "evidence" / "outputs" / run_id / "result.json").unlink()
        with pytest.raises(ValueError):
            state.verify_backup(bundle)
        shutil.rmtree(bundle)
        state.backup(bundle)
        (bundle / "manifest.json").write_text("not-json", encoding="utf-8")
        with pytest.raises(ValueError):
            state.verify_backup(bundle)
    finally:
        state.close()


def test_bundle_rejects_legacy_raw_sqlite_destination(tmp_path: Path) -> None:
    """The public backup API cannot bypass immutable bundle construction."""
    state = State(tmp_path)
    try:
        with pytest.raises(ValueError, match="bundle"):
            state.backup(state.layout.backups / "legacy.sqlite3")
    finally:
        state.close()


def test_bundle_cross_checks_snapshot_state_against_manifest_evidence(tmp_path: Path) -> None:
    """Rehashing a tampered snapshot cannot detach accepted evidence from durable state."""
    state, bundle, _ = _bundle(tmp_path)
    try:
        snapshot = bundle / "state.sqlite3"
        connection = sqlite3.connect(snapshot)
        try:
            connection.execute("DELETE FROM accepted_outputs")
            connection.commit()
        finally:
            connection.close()
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["state_snapshot"] = {
            "path": "state.sqlite3",
            "sha256": sha256_file(snapshot),
            "byte_count": snapshot.stat().st_size,
        }
        manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="manifest|evidence"):
            state.verify_backup(bundle)
    finally:
        state.close()


def test_bundle_rejects_unsafe_extra_symlink(tmp_path: Path) -> None:
    """Verification rejects unlisted symlinks rather than following archive content."""
    state, bundle, _ = _bundle(tmp_path)
    try:
        target = tmp_path / "outside"
        target.write_text("outside", encoding="utf-8")
        try:
            (bundle / "evidence" / "unsafe").symlink_to(target)
        except OSError:
            pytest.skip("symbolic links are unavailable in this Windows test environment")
        with pytest.raises(ValueError):
            state.verify_backup(bundle)
    finally:
        state.close()


def test_bundle_rejects_custom_limit_manifest_tamper(tmp_path: Path) -> None:
    """Backup verification binds completed evidence to persisted admission limits."""
    runtime = IntelligenceRuntime(
        RuntimeConfig(
            tmp_path,
            mutations_enabled=True,
            max_log_bytes=101,
            max_output_bytes=202,
            max_output_entries=3,
            max_runtime_seconds=4,
        ),
        Registry((RegisteredCommand("approved", True, bundle_success_handler, {}),)),
    )
    try:
        run_id = runtime.run("approved")
    finally:
        runtime.close()
    state = State(tmp_path)
    try:
        bundle = state.layout.backups / "custom-limits.bundle"
        state.backup(bundle)
        path = bundle / "evidence" / "manifests" / f"{run_id}.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["limits"]["max_output_bytes"] = 999
        path.write_text(canonical_json(manifest), encoding="utf-8")
        inventory_path = bundle / "manifest.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        for item in inventory["evidence"]:
            if item["path"] == f"evidence/manifests/{run_id}.json":
                item["sha256"] = sha256_file(path)
                item["byte_count"] = path.stat().st_size
                break
        inventory_path.write_text(canonical_json(inventory), encoding="utf-8")
        with pytest.raises(ValueError, match="limits"):
            state.verify_backup(bundle)
    finally:
        state.close()
