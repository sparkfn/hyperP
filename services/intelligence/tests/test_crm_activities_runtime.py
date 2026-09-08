"""Descriptor-backed candidate replay contracts for CRM activity archives."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest
from intelligence import cli as intelligence_cli
from intelligence.artifacts import canonical_json
from intelligence.artifacts_staging import scan_staged_outputs
from intelligence.config import RuntimeConfig
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities import cli as archive_cli
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
from intelligence.crm.activities.acceptance_history_io import candidate_history
from intelligence.crm.activities.bounded import ReadBudget, ReadLimits
from intelligence.crm.activities.checkpoint_limits import CheckpointLimits
from intelligence.crm.activities.config import CrmActivitiesConfig
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import ArchiveRequest, sha256_json
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


def _run(
    run_id: str,
    state: str = "completed",
    command: str = "crm_activities_extract",
) -> Run:
    return Run(run_id, command, state, 1, 1.0, 1.0)


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


def test_enabled_extract_cli_reaches_runtime_with_full_archive_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = RuntimeConfig(tmp_path, mutations_enabled=True)
    archive_config = CrmActivitiesConfig(
        "bolt://example.invalid",
        "neo4j",
        "secret",
        None,
        "bitrix-primary",
        "bitrix_chat",
        10,
        100,
        10,
        100_000,
        100,
        "db-test",
        10,
    )
    reached: list[str] = []

    class FakeRuntime:
        def __init__(self, runtime_config: RuntimeConfig, registry: object) -> None:
            del registry
            self.config = runtime_config
            self.state = _State({}, {})

        def run(self, name: str) -> str:
            reached.append(name)
            raise RuntimeError("runtime reached")

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        archive_cli.RuntimeConfig,
        "from_environment",
        classmethod(lambda _cls: config),
    )
    monkeypatch.setattr(
        archive_cli.CrmActivitiesConfig,
        "from_environment",
        classmethod(lambda _cls: archive_config),
    )
    monkeypatch.setattr(archive_cli, "IntelligenceRuntime", FakeRuntime)
    arguments = type(
        "Arguments", (), {"crm_activities_command": "extract", "checkpoint_id": "checkpoint-a"}
    )()
    with pytest.raises(RuntimeError, match="runtime reached"):
        archive_cli.main(arguments)
    assert reached == ["crm_activities_extract"]


def test_completed_descriptor_replays_and_verification_is_idempotent(tmp_path: Path) -> None:
    request, descriptor, pointer, archive_outputs = _accepted_archive(tmp_path)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(pointer)
    )
    verification_run = "verification-run"
    verification_relative = "verifications/crm/activities/snapshot-a.json"
    verification_value: dict[str, object] = {
        "schema_version": "crm-activities-verification-v1",
        "snapshot_id": "snapshot-a",
        "manifest_digest": "c" * 64,
        "verified": True,
        "accepted_run_id": pointer.run_id,
        "accepted_manifest_digest": "c" * 64,
        "accepted_descriptor_sha256": pointer.descriptor_sha256,
    }
    verification_value["digest"] = sha256_json(verification_value)
    artifact = canonical_json(verification_value).encode("utf-8")
    artifact_path = tmp_path / "outputs" / verification_run / verification_relative
    artifact_path.parent.mkdir(mode=0o700, parents=True)
    artifact_path.write_bytes(artifact)
    verification_inventory = OutputInventory(
        f"outputs/{verification_run}/{verification_relative}",
        sha256(artifact).hexdigest(),
        len(artifact),
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
                    "relative_path": verification_relative,
                    "sha256": verification_inventory.sha256,
                    "byte_count": verification_inventory.byte_count,
                }
            ],
        },
    )
    runtime = _Runtime(
        tmp_path,
        {
            pointer.run_id: _run(pointer.run_id),
            verification_run: _run(verification_run, command="crm_activities_verify"),
        },
        {pointer.run_id: archive_outputs, verification_run: (verification_inventory,)},
    )
    fake = cast(IntelligenceRuntime, runtime)
    assert publication(fake, request) == descriptor
    assert publication(fake, request) == descriptor
    assert verification(fake, request.snapshot_id)["run_id"] == verification_run
    assert verification(fake, request.snapshot_id)["run_id"] == verification_run


def test_extraction_output_cannot_self_establish_verification(tmp_path: Path) -> None:
    request, _, pointer, archive_outputs = _accepted_archive(tmp_path)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(pointer)
    )
    _write_candidate(
        root,
        verification_candidate_name(pointer.run_id),
        {
            "checkpoint_id": request.snapshot_id,
            "run_id": pointer.run_id,
            "accepted_run_id": pointer.run_id,
            "snapshot_id": "snapshot-a",
            "accepted_manifest_digest": "c" * 64,
            "accepted_descriptor_sha256": pointer.descriptor_sha256,
            "inventory": [],
        },
    )
    runtime = _Runtime(
        tmp_path,
        {pointer.run_id: _run(pointer.run_id)},
        {pointer.run_id: archive_outputs},
    )
    with pytest.raises(RuntimeError, match="run command"):
        verification(cast(IntelligenceRuntime, runtime), request.snapshot_id)


def test_candidate_history_charges_entries_before_materializing_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "staging" / ".crm-activities" / "checkpoint-a"
    root.mkdir(parents=True)
    for index in range(20):
        (root / f"unrelated-{index:02d}.json").write_text("{}", encoding="utf-8")
    observed = 0
    original = Path.iterdir

    def bounded_iterdir(path: Path) -> Iterator[Path]:
        nonlocal observed
        for item in original(path):
            if path == root:
                observed += 1
            yield item

    monkeypatch.setattr(Path, "iterdir", bounded_iterdir)
    with pytest.raises(RuntimeError, match="entry ceiling"):
        candidate_history(
            tmp_path,
            "checkpoint-a",
            "publication-candidate-",
            ReadBudget(ReadLimits(1_000, 1, 10)),
        )
    assert observed == 2


def test_candidate_filename_is_bound_to_embedded_run_identity(tmp_path: Path) -> None:
    request, _, pointer, outputs = _accepted_archive(tmp_path)
    root = tmp_path / "staging" / ".crm-activities" / request.snapshot_id
    _write_candidate(
        root,
        "publication-candidate-" + "0" * 64 + ".json",
        publication_candidate(pointer),
    )
    runtime = _Runtime(tmp_path, {pointer.run_id: _run(pointer.run_id)}, {pointer.run_id: outputs})
    with pytest.raises(ValueError, match="name conflicts"):
        publication(cast(IntelligenceRuntime, runtime), request)


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
    result = status(tmp_path, request.snapshot_id)
    assert result["state"]["phase"] == "completed"
    assert result["manifest_digest"] == manifest["digest"]


def test_status_selects_completed_attempt_without_erasing_failed_history(tmp_path: Path) -> None:
    request = ArchiveRequest("checkpoint-a", "bitrix-primary")
    boundary = seal((), request)
    staging = tmp_path / "staging" / "archive-success"
    staging.mkdir(parents=True)
    manifest = write_snapshot(staging, boundary, (), ())
    snapshot_outputs = scan_staged_outputs(tmp_path, staging.name, 1_000_000, 100)
    descriptor = publication_descriptor(
        request.snapshot_id,
        request,
        boundary.digest,
        staging.name,
        "crm_activities_extract",
        boundary.logical_snapshot_id,
        str(manifest["digest"]),
        str(manifest["cleanup_identity_digest"]),
        snapshot_outputs,
    )
    pointer = write_publication_descriptor(staging, descriptor)
    shutil.copytree(staging, tmp_path / "outputs" / staging.name)
    outputs = tuple(
        sorted(
            (
                OutputInventory(
                    f"outputs/{staging.name}/{item.relative_path}",
                    item.sha256,
                    item.byte_count,
                )
                for item in scan_staged_outputs(tmp_path, staging.name, 1_000_000, 100)
            ),
            key=lambda item: item.relative_path,
        )
    )
    limits = CheckpointLimits(max_bytes=1_000_000, max_entries=100)
    root = checkpoints.checkpoint_root(staging, request.snapshot_id, limits)
    checkpoints.initialize(root, request, limits)
    checkpoints.write_boundary(root, boundary, limits)
    checkpoints.write_evidence(root, "accepted-manifest.json", manifest, limits)
    checkpoints.complete(root, boundary.digest, 0, limits)
    failed = PublicationPointer(
        "archive-failed",
        pointer.descriptor_relative_path,
        pointer.descriptor_sha256,
        pointer.descriptor_byte_count,
    )
    _write_candidate(root, publication_candidate_name(failed.run_id), publication_candidate(failed))
    _write_candidate(
        root, publication_candidate_name(pointer.run_id), publication_candidate(pointer)
    )
    fake_state = _State(
        {failed.run_id: _run(failed.run_id, "failed"), pointer.run_id: _run(pointer.run_id)},
        {pointer.run_id: outputs},
    )
    value = status(tmp_path, request.snapshot_id, state=fake_state)
    assert value["accepted_run"] == descriptor
    attempts = value["publication_attempts"]
    assert {item["state"] for item in attempts} == {"completed", "failed"}
