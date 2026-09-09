"""Canonical temporary accepted #353/#354 dependency admission contracts."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from intelligence.artifacts_staging import publish_inventory, scan_staged_outputs
from intelligence.crm.activities.acceptance import (
    publication_candidate,
    publication_candidate_name,
    publication_descriptor,
    write_publication_descriptor,
)
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import ArchiveRecord, ArchiveRequest, ParentReference
from intelligence.crm.activities.reconciliation import seal
from intelligence.crm.activities.snapshot_verifier import snapshot_inventory
from intelligence.crm_deal_refs.export import export_snapshot, seal_boundary
from intelligence.datasets.admission import admit, admit_activities, admit_deals
from intelligence.datasets.models import DatasetRequest
from intelligence.models import OutputInventory
from intelligence.state import State
from test_crm_deal_refs_export import FakeRepository


@dataclass(frozen=True)
class _Config:
    workspace: Path


@dataclass(frozen=True)
class _Runtime:
    config: _Config
    state: State


def _publish(state: State, command: str, writer: object) -> str:
    run = state.create_mutating_run(command)
    staging = state.layout.staging / run.run_id
    staging.mkdir(mode=0o700, parents=True)
    assert callable(writer)
    writer(staging, run.run_id)
    inventory = scan_staged_outputs(state.workspace, run.run_id, 100_000_000)
    state.mark_execution_quiescent(run)
    state.begin_publishing(run, inventory)
    published = publish_inventory(state.workspace, run.run_id, inventory, 100_000_000)
    state.complete_publication(run, published, {"synthetic": command})
    return run.run_id


def _deal_run(
    state: State,
    command: str = "crm_deal_refs_extract",
    source_instance_id: str = "instance-a",
) -> str:
    def writer(staging: Path, _run_id: str) -> None:
        class SourceRepository(FakeRepository):
            def validate_source_instance(self, value: str) -> None:
                assert value == source_instance_id

        boundary, deals, identities = seal_boundary(
            SourceRepository(),
            source_instance_id=source_instance_id,
            as_of="2026-02-01T00:00:00Z",
            page_size=1,
            max_records=2,
        )
        export_snapshot(staging, boundary, deals, identities)

    return _publish(state, command, writer)


def _activity_record() -> ArchiveRecord:
    return ArchiveRecord(
        "activity-a",
        "history-a",
        "1",
        "history-a-v1",
        "hash-a",
        "instance-a",
        "bitrix_chat",
        "crm_history",
        "active",
        "activity",
        "call",
        "bitrix_crm_activity",
        "2",
        "bitrix_crm_activity_v2",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        ParentReference(
            None,
            "instance-a",
            "bitrix-crm-deal-42",
            "crm_deal",
            "STORED_PARENT",
            "bitrix_chat",
        ),
        (),
        (),
        (),
        (),
        "2026-01-01T00:00:00Z",
        None,
    )


def _activity_run(state: State) -> tuple[str, str]:
    request = ArchiveRequest("checkpoint-a", "instance-a", database_identity="synthetic")
    record = _activity_record()
    boundary = seal((record,), request)
    snapshot_id = boundary.logical_snapshot_id

    def writer(staging: Path, run_id: str) -> None:
        write_snapshot(staging, boundary, (record,), classify((record,)))
        root = staging / "snapshots" / "crm" / "activities" / snapshot_id
        inventory = tuple(
            OutputInventory(f"snapshots/crm/activities/{snapshot_id}/{path}", digest, count)
            for path, digest, count in snapshot_inventory(root)
        )
        descriptor = publication_descriptor(
            request.snapshot_id,
            request,
            boundary.digest,
            run_id,
            "crm_activities_extract",
            snapshot_id,
            str(__import__("json").loads((root / "manifest.json").read_text())["digest"]),
            "0" * 64,
            inventory,
        )
        pointer = write_publication_descriptor(staging, descriptor)
        candidate = state.workspace / "staging" / ".crm-activities" / request.snapshot_id
        candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        (candidate / publication_candidate_name(run_id)).write_text(
            __import__("intelligence.artifacts", fromlist=["canonical_json"]).canonical_json(
                publication_candidate(pointer)
            ),
            encoding="utf-8",
        )

    return _publish(state, "crm_activities_extract", writer), snapshot_id


def test_real_state_completed_wrong_command_and_unregistered_deal_admission(tmp_path: Path) -> None:
    state = State(tmp_path)
    try:
        run_id = _deal_run(state)
        assert admit_deals(tmp_path, state, run_id).run_id == run_id
        wrong = _deal_run(state, "dataset_build")
        with pytest.raises(ValueError, match="completed accepted"):
            admit_deals(tmp_path, state, wrong)
        state.connection.execute("DELETE FROM accepted_outputs WHERE run_id = ?", (run_id,))
        with pytest.raises(ValueError):
            admit_deals(tmp_path, state, run_id)
    finally:
        state.close()


def test_activity_manifest_checksum_is_proven_before_selected_count(tmp_path: Path) -> None:
    state = State(tmp_path)
    try:
        run_id, snapshot_id = _activity_run(state)
        runtime = _Runtime(_Config(tmp_path), state)
        assert admit_activities(runtime, "checkpoint-a").accepted_run_id == run_id
        manifest = (
            tmp_path
            / "outputs"
            / run_id
            / "snapshots"
            / "crm"
            / "activities"
            / snapshot_id
            / "manifest.json"
        )
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                '"selected_count":1', '"selected_count":2'
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="checksum"):
            admit_activities(runtime, "checkpoint-a")
    finally:
        state.close()


def test_public_admit_rejects_missing_publication_pinned_run_and_source_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = State(tmp_path)
    try:
        deal_run = _deal_run(state)
        runtime = _Runtime(_Config(tmp_path), state)
        request = DatasetRequest(
            deal_run,
            "checkpoint-a",
            "missing-run",
            "crm-deal-state-v1",
            "2026-01-01T12:00:00Z",
            "2026-01-02T12:00:00Z",
            1,
        )
        with pytest.raises(ValueError, match="no State-accepted"):
            admit(runtime, request)
        activity_run, _snapshot = _activity_run(state)
        mismatch = DatasetRequest(
            deal_run,
            "checkpoint-a",
            "wrong-run",
            "crm-deal-state-v1",
            "2026-01-01T12:00:00Z",
            "2026-01-02T12:00:00Z",
            1,
        )
        with pytest.raises(ValueError, match="accepted run"):
            admit(runtime, mismatch)
        admitted_deals = admit_deals(tmp_path, state, deal_run)
        changed_boundary = replace(admitted_deals.boundary, source_instance_id="instance-b")
        monkeypatch.setattr(
            "intelligence.datasets.admission.admit_deals",
            lambda *_args: replace(admitted_deals, boundary=changed_boundary),
        )
        with pytest.raises(ValueError, match="source instance"):
            admit(
                _Runtime(_Config(tmp_path), state),
                DatasetRequest(
                    deal_run,
                    "checkpoint-a",
                    activity_run,
                    "crm-deal-state-v1",
                    "2026-01-01T12:00:00Z",
                    "2026-01-02T12:00:00Z",
                    1,
                ),
            )
    finally:
        state.close()
