"""Exact accepted-run archive admission failure tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from intelligence.crm.activities.cleanup import admission
from intelligence.crm.activities.cleanup.models import (
    CleanupAuthorization,
    CleanupRequest,
    CleanupTarget,
)
from intelligence.models import OutputInventory


def _request(manifest: str = "a" * 64) -> CleanupRequest:
    return CleanupRequest(
        CleanupAuthorization("checkpoint-a", "run-a", "snapshot-a", manifest),
        CleanupTarget("environment-a", "database-a"),
        1,
    )


def _snapshot(tmp_path: Path, include_record: bool = True) -> Path:
    snapshot = tmp_path / "snapshot"
    records = snapshot / "records"
    records.mkdir(parents=True)
    cleanup = {
        "identities": [
            {
                "source_record_pk": "source-a",
                "record_type": "crm_history",
                "source_record_version": "v1",
                "record_hash": "hash-a",
                "lifecycle_status": "active",
            }
        ]
    }
    (snapshot / "cleanup-identities.json").write_text(json.dumps(cleanup), encoding="utf-8")
    page = {
        "records": []
        if not include_record
        else [
            {
                "source_record_pk": "source-a",
                "record_type": "crm_history",
                "source_record_version": "v1",
                "record_hash": "hash-a",
                "lifecycle_status": "active",
                "source_instance_id": "bitrix-a",
                "source_key": "bitrix_chat",
                "history_family": "activity",
                "stored_parent": {},
            }
        ]
    }
    (records / "page-00000001.json").write_text(json.dumps(page), encoding="utf-8")
    return snapshot


def test_admission_rejects_locator_conflict_before_snapshot_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    descriptor = SimpleNamespace(snapshot_id="snapshot-a", manifest_digest="b" * 64)
    monkeypatch.setattr(
        admission, "accepted_publication_for_run", lambda *_args: (descriptor, object())
    )
    with pytest.raises(RuntimeError, match="locators conflict"):
        admission.admit(
            SimpleNamespace(config=SimpleNamespace(workspace=tmp_path), state=object()), _request()
        )


def test_admission_rejects_missing_record_join_and_state_inventory_tampering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(tmp_path, include_record=False)
    pointer = SimpleNamespace(
        descriptor_relative_path="descriptor.json",
        descriptor_sha256="c" * 64,
        descriptor_byte_count=1,
    )
    descriptor = SimpleNamespace(
        run_id="run-a", snapshot_id="snapshot-a", manifest_digest="a" * 64, snapshot_inventory=()
    )
    monkeypatch.setattr(
        admission, "accepted_publication_for_run", lambda *_args: (descriptor, pointer)
    )
    monkeypatch.setattr(admission, "accepted_snapshot", lambda *_args: snapshot)
    monkeypatch.setattr(admission, "verify_snapshot", lambda *_args: {"manifest_digest": "a" * 64})
    monkeypatch.setattr(admission, "snapshot_inventory", lambda *_args: ())
    state = SimpleNamespace(accepted_outputs=lambda _run: (OutputInventory("wrong", "d" * 64, 1),))
    runtime = SimpleNamespace(config=SimpleNamespace(workspace=tmp_path), state=state)
    with pytest.raises(RuntimeError, match="State inventory"):
        admission.admit(runtime, _request())
    expected = OutputInventory("outputs/run-a/descriptor.json", "c" * 64, 1)
    runtime = SimpleNamespace(
        config=SimpleNamespace(workspace=tmp_path),
        state=SimpleNamespace(accepted_outputs=lambda _run: (expected,)),
    )
    with pytest.raises(RuntimeError, match="does not join accepted record evidence"):
        admission.admit(runtime, _request())
