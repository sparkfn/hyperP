"""Candidate replay contracts for the supervised CRM activities runtime adapter."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from intelligence import cli as intelligence_cli
from intelligence.crm.activities import checkpoints
from intelligence.crm.activities.acceptance import publication, verification
from intelligence.crm.activities.models import ArchiveRequest
from intelligence.models import OutputInventory, Run
from intelligence.runtime import IntelligenceRuntime


class _Config:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace


class _State:
    def __init__(self, outputs: dict[str, tuple[OutputInventory, ...]]) -> None:
        self._outputs = outputs

    def inspect(self, run_id: str) -> Run | None:
        return (
            Run(run_id, "crm_activities", "completed", 1, 1.0, 1.0)
            if run_id in self._outputs
            else None
        )

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return self._outputs[run_id]


class _Runtime:
    def __init__(self, workspace: Path, outputs: dict[str, tuple[OutputInventory, ...]]) -> None:
        self.config = _Config(workspace)
        self.state = _State(outputs)


def _candidate_root(workspace: Path, checkpoint_id: str) -> Path:
    run_staging = workspace / "staging" / "candidate-writer"
    run_staging.mkdir(parents=True)
    return checkpoints.checkpoint_root(run_staging, checkpoint_id)


def _publication_candidate(checkpoint_id: str, run_id: str) -> dict[str, object]:
    request = ArchiveRequest(checkpoint_id, "bitrix-primary")
    return {
        "checkpoint_id": checkpoint_id,
        "request": request.as_public_dict(),
        "boundary_digest": "b" * 64,
        "run_id": run_id,
        "snapshot_id": "snapshot-a",
        "manifest_digest": "c" * 64,
        "cleanup_identity_digest": "d" * 64,
        "inventory": [
            {
                "relative_path": "snapshots/crm/activities/snapshot-a/manifest.json",
                "sha256": "e" * 64,
                "byte_count": 2,
            }
        ],
    }


def _verification_candidate(checkpoint_id: str, run_id: str) -> dict[str, object]:
    return {
        "checkpoint_id": checkpoint_id,
        "run_id": run_id,
        "accepted_run_id": "archive-run",
        "snapshot_id": "snapshot-a",
        "accepted_manifest_digest": "c" * 64,
        "inventory": [
            {
                "relative_path": "verifications/crm/activities/snapshot-a.json",
                "sha256": "f" * 64,
                "byte_count": 3,
            }
        ],
    }


def _outputs(run_id: str, candidate: dict[str, object]) -> tuple[OutputInventory, ...]:
    inventory = candidate["inventory"]
    assert isinstance(inventory, list)
    result: list[OutputInventory] = []
    for item in inventory:
        assert isinstance(item, dict)
        path, digest, count = item["relative_path"], item["sha256"], item["byte_count"]
        assert isinstance(path, str) and isinstance(digest, str) and isinstance(count, int)
        result.append(OutputInventory(f"outputs/{run_id}/{path}", digest, count))
    return tuple(result)


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
    assert not (tmp_path / "state").exists()


def test_repeated_candidate_replay_uses_completed_state_without_checkpoint_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkpoint_id = "checkpoint-a"
    root = _candidate_root(tmp_path, checkpoint_id)
    archive = _publication_candidate(checkpoint_id, "archive-run")
    verified = _verification_candidate(checkpoint_id, "verification-run")
    checkpoints.write_evidence(root, "publication-candidate.json", archive)
    checkpoints.write_evidence(root, "verification-candidate.json", verified)
    runtime = _Runtime(
        tmp_path,
        {
            "archive-run": _outputs("archive-run", archive),
            "verification-run": _outputs("verification-run", verified),
        },
    )

    def no_checkpoint_writes(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("candidate replay must not write checkpoint evidence")

    monkeypatch.setattr(checkpoints, "write_evidence", no_checkpoint_writes)
    fake = cast(IntelligenceRuntime, runtime)
    assert publication(fake, checkpoint_id) == archive
    assert publication(fake, checkpoint_id) == archive
    assert verification(fake, checkpoint_id) == verified
    assert verification(fake, checkpoint_id) == verified


def test_candidate_inventory_conflict_is_rejected_without_snapshot_read(tmp_path: Path) -> None:
    checkpoint_id = "checkpoint-a"
    root = _candidate_root(tmp_path, checkpoint_id)
    candidate = _publication_candidate(checkpoint_id, "archive-run")
    checkpoints.write_evidence(root, "publication-candidate.json", candidate)
    runtime = _Runtime(
        tmp_path,
        {"archive-run": (OutputInventory("outputs/archive-run/other.json", "a" * 64, 1),)},
    )
    with pytest.raises(RuntimeError, match="inventory conflicts"):
        publication(cast(IntelligenceRuntime, runtime), checkpoint_id)
