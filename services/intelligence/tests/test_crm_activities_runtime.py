"""Descriptor-backed candidate replay contracts for CRM activity archives."""

from __future__ import annotations

import shutil
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from intelligence import cli as intelligence_cli
from intelligence.artifacts import canonical_json
from intelligence.artifacts_staging import scan_staged_outputs
from intelligence.crm.activities.acceptance import (
    PublicationPointer,
    publication,
    publication_candidate,
    publication_candidate_name,
    publication_descriptor,
    verification,
    verification_candidate_name,
    write_publication_descriptor,
)
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import ArchiveRequest
from intelligence.crm.activities.reconciliation import seal
from intelligence.crm.activities.status import status
from intelligence.models import OutputInventory, Run
from intelligence.runtime import IntelligenceRuntime


class _Config:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace


class _State:
    def __init__(
        self, runs: dict[str, Run], outputs: dict[str, tuple[OutputInventory, ...]]
    ) -> None:
        self._runs = runs
        self._outputs = outputs

    def inspect(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return self._outputs[run_id]


class _Runtime:
    def __init__(
        self,
        workspace: Path,
        runs: dict[str, Run],
        outputs: dict[str, tuple[OutputInventory, ...]],
    ) -> None:
        self.config = _Config(workspace)
        self.state = _State(runs, outputs)


def _run(run_id: str, state: str = "completed") -> Run:
    return Run(run_id, "crm_activities_extract", state, 1, 1.0, 1.0)


def _write_candidate(root: Path, name: str, value: dict[str, object]) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / name).write_bytes(canonical_json(value).encode("utf-8"))


def _write_json(root: Path, name: str, value: dict[str, object]) -> None:
    (root / name).write_bytes(canonical_json(value).encode("utf-8"))


def _accepted_archive(
    workspace: Path,
    run_id: str = "archive-run",
) -> tuple[ArchiveRequest, dict[str, object], PublicationPointer, tuple[OutputInventory, ...]]:
    checkpoint_id = "checkpoint-a"
    request = ArchiveRequest(checkpoint_id, "bitrix-primary", database_identity="neo4j-a")
    staging = workspace / "staging" / run_id
    snapshot_path = staging / "snapshots" / "crm" / "activities" / "snapshot-a" / "manifest.json"
    snapshot_path.parent.mkdir(mode=0o700, parents=True)
    snapshot_bytes = b"{}"
    snapshot_path.write_bytes(snapshot_bytes)
    snapshot = OutputInventory(
        "snapshots/crm/activities/snapshot-a/manifest.json",
        sha256(snapshot_bytes).hexdigest(),
        len(snapshot_bytes),
    )
    descriptor = publication_descriptor(
        checkpoint_id,
        request,
        "b" * 64,
        run_id,
        "crm_activities_extract",
        "snapshot-a",
        "c" * 64,
        "d" * 64,
        (snapshot,),
    )
    pointer = write_publication_descriptor(staging, descriptor)
    output = workspace / "outputs" / run_id
    shutil.copytree(staging, output)
    descriptor_output = OutputInventory(
        f"outputs/{run_id}/{pointer.descriptor_relative_path}",
        pointer.descriptor_sha256,
        pointer.descriptor_byte_count,
    )
    snapshot_output = OutputInventory(
        f"outputs/{run_id}/{snapshot.relative_path}", snapshot.sha256, snapshot.byte_count
    )
    return (
        request,
        descriptor,
        pointer,
        tuple(sorted((descriptor_output, snapshot_output), key=lambda item: item.relative_path)),
    )


def test_root_cli_imports_activities_and_is_default_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INTELLIGENCE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("INTELLIGENCE_MUTATIONS_ENABLED", raising=False)
    parsed = intelligence_cli.build_parser().parse_args(
        ("crm", "activities", "extract", "--snapshot-id", "checkpoint-a")
    )
    assert parsed.crm_activities_command == "extract"
    with pytest.raises(RuntimeError, match="mutating execution is disabled"):
        intelligence_cli.main(("crm", "activities", "extract", "--snapshot-id", "checkpoint-a"))


def test_completed_descriptor_replays_and_verification_is_idempotent(tmp_path: Path) -> None:
    request, descriptor, pointer, archive_outputs = _accepted_archive(tmp_path)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(pointer)
    )
    verification_run = "verification-run"
    verification_inventory = OutputInventory(
        "outputs/verification-run/verifications/crm/activities/snapshot-a.json", "f" * 64, 3
    )
    _write_candidate(
        root,
        verification_candidate_name(verification_run),
        {
            "checkpoint_id": request.snapshot_id,
            "run_id": verification_run,
            "accepted_run_id": pointer.run_id,
            "snapshot_id": "snapshot-a",
            "accepted_manifest_digest": "c" * 64,
            "accepted_descriptor_sha256": pointer.descriptor_sha256,
            "inventory": [
                {
                    "relative_path": "verifications/crm/activities/snapshot-a.json",
                    "sha256": "f" * 64,
                    "byte_count": 3,
                }
            ],
        },
    )
    runtime = _Runtime(
        tmp_path,
        {pointer.run_id: _run(pointer.run_id), verification_run: _run(verification_run)},
        {pointer.run_id: archive_outputs, verification_run: (verification_inventory,)},
    )
    fake = cast(IntelligenceRuntime, runtime)
    assert publication(fake, request) == descriptor
    assert publication(fake, request) == descriptor
    assert verification(fake, request.snapshot_id)["run_id"] == verification_run
    assert verification(fake, request.snapshot_id)["run_id"] == verification_run


def test_tampered_publication_pointer_is_not_accepted(tmp_path: Path) -> None:
    request, _, pointer, outputs = _accepted_archive(tmp_path)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    tampered = PublicationPointer(
        pointer.run_id,
        pointer.descriptor_relative_path,
        "a" * 64,
        pointer.descriptor_byte_count,
    )
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(tampered)
    )
    runtime = _Runtime(tmp_path, {pointer.run_id: _run(pointer.run_id)}, {pointer.run_id: outputs})
    with pytest.raises(RuntimeError, match="not registered"):
        publication(cast(IntelligenceRuntime, runtime), request)


def test_failed_candidate_is_replaced_by_later_completed_attempt(tmp_path: Path) -> None:
    request, descriptor, pointer, outputs = _accepted_archive(tmp_path, "archive-success")
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    failed = PublicationPointer(
        "archive-failed", pointer.descriptor_relative_path, "a" * 64, pointer.descriptor_byte_count
    )
    _write_candidate(root, publication_candidate_name(failed.run_id), publication_candidate(failed))
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(pointer)
    )
    runtime = _Runtime(
        tmp_path,
        {failed.run_id: _run(failed.run_id, "failed"), pointer.run_id: _run(pointer.run_id)},
        {pointer.run_id: outputs},
    )
    assert publication(cast(IntelligenceRuntime, runtime), request) == descriptor


def test_request_database_drift_does_not_reuse_completed_descriptor(tmp_path: Path) -> None:
    request, _, pointer, outputs = _accepted_archive(tmp_path)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(pointer)
    )
    runtime = _Runtime(tmp_path, {pointer.run_id: _run(pointer.run_id)}, {pointer.run_id: outputs})
    drifted = ArchiveRequest(
        request.snapshot_id, request.source_instance_id, database_identity="neo4j-b"
    )
    assert publication(cast(IntelligenceRuntime, runtime), drifted) is None


def test_descriptor_writer_integrates_with_bounded_status(tmp_path: Path) -> None:
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    boundary = seal((), request)
    run = tmp_path / "staging" / "archive-run"
    run.mkdir(parents=True)
    manifest = write_snapshot(run, boundary, (), ())
    inventory = scan_staged_outputs(tmp_path, run.name, 1_000_000, 100)
    descriptor = publication_descriptor(
        request.snapshot_id,
        request,
        boundary.digest,
        run.name,
        "crm_activities_extract",
        boundary.logical_snapshot_id,
        str(manifest["digest"]),
        str(manifest["cleanup_identity_digest"]),
        inventory,
    )
    pointer = write_publication_descriptor(run, descriptor)
    shutil.copytree(run, tmp_path / "outputs" / run.name)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    (root / "records").mkdir(parents=True)
    (root / "pages").mkdir()
    _write_json(root, "request.json", request.as_public_dict())
    _write_json(
        root,
        "checkpoint.json",
        {
            "schema_version": "crm-activities-checkpoint-v1",
            "phase": "completed",
            "request_digest": sha256(
                canonical_json(request.as_public_dict()).encode("utf-8")
            ).hexdigest(),
            "boundary_digest": boundary.digest,
            "pages": 0,
        },
    )
    _write_json(root, "boundary.json", boundary.as_dict())
    _write_json(root, "accepted-manifest.json", dict(manifest))
    _write_candidate(root, publication_candidate_name(run.name), publication_candidate(pointer))
    result = status(tmp_path, request.snapshot_id)
    assert result["accepted_run"] == descriptor
    assert result["publication_candidate"] == pointer.as_dict()
