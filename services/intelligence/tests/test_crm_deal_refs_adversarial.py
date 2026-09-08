"""Adversarial replay, verification, registry, status, and repository bounds."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm_deal_refs import cli as deal_cli
from intelligence.crm_deal_refs.commands import ResumeRequest, resume_handler
from intelligence.crm_deal_refs.export import (
    export_snapshot,
    seal_boundary,
    verified_snapshot_inventory,
    verify_snapshot,
)
from intelligence.crm_deal_refs.models import MAX_RECORDS, Boundary, canonical_digest
from intelligence.models import OutputInventory, Run
from intelligence.repositories.neo4j.crm_deal_refs import Neo4jCrmDealRefsRepository
from test_crm_deal_refs_export import FakeRepository


class ResolvedRepository(FakeRepository):
    def list_identity_revisions(self, *_args: object) -> list[dict[str, object]]:
        return [
            {
                "event_id": "event-1",
                "global_revision": 1,
                "source_instance_id": "instance-a",
                "source_entity_id": "42",
                "identity_policy_version": "crm_deal_identity_v2",
                "link_status": "resolved",
                "hyperp_person_id": "00000000-0000-0000-0000-000000000001",
                "person_status": "active",
                "resolution_kind": "baseline",
                "resolution_revision": 1,
                "effective_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ]


def _complete(root: Path, *, resolved: bool = False) -> tuple[Path, Boundary]:
    repository = ResolvedRepository() if resolved else FakeRepository()
    boundary, deals, identities = seal_boundary(
        repository,
        source_instance_id="instance-a",
        as_of="2026-02-01T00:00:00Z",
        page_size=1,
        max_records=2,
    )
    export_snapshot(root, boundary, deals, identities)
    return root / "snapshots" / "crm" / "deal-refs", boundary


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: dict[str, object]) -> None:
    path.write_text(canonical_json(value), encoding="utf-8")


def test_resolved_capture_uses_one_boundary_timestamp_and_verifies(tmp_path: Path) -> None:
    root, boundary = _complete(tmp_path, resolved=True)
    page = root / "pages" / "identity-revisions" / "page-000001.ndjson"
    row = json.loads(page.read_text(encoding="utf-8"))
    assert row["person_observation_captured_at"] == boundary.captured_at
    assert row["person_reference_eligible"] is True
    assert verify_snapshot(root)["identity_record_count"] == 1


def test_completed_replay_is_artifact_only_after_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    source, _ = _complete(workspace / "outputs" / "prior")
    assert source == workspace / "outputs" / "prior" / "snapshots" / "crm" / "deal-refs"
    monkeypatch.setattr(
        "intelligence.crm_deal_refs.commands.get_crm_deal_refs_repository",
        lambda: (_ for _ in ()).throw(AssertionError("Neo4j must not be opened")),
    )
    staging = workspace / "staging" / "new"
    staging.mkdir(parents=True)
    resume_handler(staging, lambda: False, ResumeRequest("prior", True))
    target = staging / "snapshots" / "crm" / "deal-refs"
    assert verify_snapshot(target) == verify_snapshot(source)


def test_rehashed_row_tampering_is_rejected_by_boundary_fingerprint(tmp_path: Path) -> None:
    root, _ = _complete(tmp_path)
    page = root / "pages" / "deal-references" / "page-000001.ndjson"
    row = json.loads(page.read_text(encoding="utf-8"))
    row["lifecycle_status_observed"] = "superseded"
    page.write_text(canonical_json(row) + "\n", encoding="utf-8")
    sidecar = root / "manifests" / "deal-references" / "page-000001.json"
    manifest = _read(sidecar)
    manifest["sha256"] = sha256_file(page)
    manifest["byte_count"] = page.stat().st_size
    _write(sidecar, manifest)
    snapshot_path = root / "snapshot-manifest.json"
    snapshot = _read(snapshot_path)
    manifests = snapshot["page_manifests"]
    assert isinstance(manifests, list) and isinstance(manifests[0], dict)
    manifests[0] = manifest
    snapshot["page_manifests_sha256"] = canonical_digest(manifests)
    _write(snapshot_path, snapshot)
    with pytest.raises(ValueError, match="fingerprints"):
        verify_snapshot(root)


@pytest.mark.parametrize(
    ("target", "field", "value"),
    (
        ("boundary.json", "source_membership_count", 2),
        ("boundary.json", "source_terminal_key", None),
        ("boundary.json", "page_size", 0),
        ("boundary.json", "max_records", MAX_RECORDS + 1),
        ("boundary.json", "as_of", "2026-02-01T08:00:00+08:00"),
        ("boundary.json", "immutable_facts_sha256", "d" * 64),
        ("checkpoint.json", "deal_records", 2),
        ("checkpoint.json", "deal_next_cursor", "wrong"),
        ("snapshot-manifest.json", "deal_record_count", 2),
        ("snapshot-manifest.json", "boundary_sha256", "b" * 64),
    ),
)
def test_boundary_checkpoint_and_snapshot_tampering_fails(
    tmp_path: Path, target: str, field: str, value: object
) -> None:
    root, _ = _complete(tmp_path)
    path = root / target
    data = _read(path)
    data[field] = value
    _write(path, data)
    with pytest.raises(ValueError):
        verify_snapshot(root)


def test_page_path_cursor_and_boundary_digest_tampering_fails(tmp_path: Path) -> None:
    for field, value in (
        ("path", "pages/deal-references/wrong.ndjson"),
        ("next_cursor", "wrong"),
        ("boundary_sha256", "c" * 64),
    ):
        case = tmp_path / field
        root, _ = _complete(case)
        sidecar = root / "manifests" / "deal-references" / "page-000001.json"
        manifest = _read(sidecar)
        manifest[field] = value
        _write(sidecar, manifest)
        snapshot_path = root / "snapshot-manifest.json"
        snapshot = _read(snapshot_path)
        manifests = snapshot["page_manifests"]
        assert isinstance(manifests, list)
        manifests[0] = manifest
        snapshot["page_manifests_sha256"] = canonical_digest(manifests)
        _write(snapshot_path, snapshot)
        with pytest.raises(ValueError):
            verify_snapshot(root)


def test_hardlink_evidence_fails_when_supported(tmp_path: Path) -> None:
    root, _ = _complete(tmp_path)
    page = root / "pages" / "deal-references" / "page-000001.ndjson"
    hard = root / "hard-link.ndjson"
    try:
        os.link(page, hard)
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError, match="hard-linked|unsafe|inventory"):
        verify_snapshot(root)


def test_symlink_evidence_and_status_are_not_accepted_when_supported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _ = _complete(tmp_path / "target")
    workspace = tmp_path / "workspace"
    link = workspace / "outputs" / "run-1" / "snapshots" / "crm" / "deal-refs"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    outputs = _registered(target)
    runtime = _runtime(workspace, _run(), outputs)
    arguments = SimpleNamespace(deal_refs_command="status", run_id="run-1")
    assert deal_cli.run_crm_deal_refs(arguments, runtime) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] is False


class _State:
    def __init__(self, run: Run, outputs: tuple[OutputInventory, ...]) -> None:
        self.run = run
        self.outputs = outputs

    def inspect(self, _run_id: str) -> Run:
        return self.run

    def accepted_outputs(self, _run_id: str) -> tuple[OutputInventory, ...]:
        return self.outputs


def _runtime(workspace: Path, run: Run, outputs: tuple[OutputInventory, ...]) -> object:
    return SimpleNamespace(config=SimpleNamespace(workspace=workspace), state=_State(run, outputs))


def _run(command: str = "crm_deal_refs_extract") -> Run:
    return Run("run-1", command, "completed", 1, 0.0, 0.0)


def _registered(root: Path) -> tuple[OutputInventory, ...]:
    prefix = "outputs/run-1/snapshots/crm/deal-refs/"
    return tuple(
        OutputInventory(prefix + path, digest, size)
        for path, digest, size in verified_snapshot_inventory(root)
    )


def test_accepted_registry_command_path_hash_size_and_set_mismatches_fail(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    root, _ = _complete(workspace / "outputs" / "run-1")
    outputs = _registered(root)
    deal_cli._require_accepted_output(_runtime(workspace, _run(), outputs), "run-1", root)
    bad_cases = (
        (_run("other"), outputs),
        (_run(), outputs[:-1]),
        (_run(), outputs + (OutputInventory("outputs/run-1/extra", "a" * 64, 1),)),
        (_run(), (replace(outputs[0], relative_path="outputs/run-1/wrong"), *outputs[1:])),
        (_run(), (replace(outputs[0], sha256="b" * 64), *outputs[1:])),
        (_run(), (replace(outputs[0], byte_count=outputs[0].byte_count + 1), *outputs[1:])),
    )
    for run, inventory in bad_cases:
        with pytest.raises(ValueError):
            deal_cli._require_accepted_output(
                _runtime(workspace, run, tuple(inventory)), "run-1", root
            )


def test_status_reports_malformed_evidence_as_not_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "workspace"
    partial = workspace / "staging" / "run-1" / "snapshots" / "crm" / "deal-refs"
    partial.mkdir(parents=True)
    (partial / "checkpoint.json").write_text("{}", encoding="utf-8")
    runtime = _runtime(workspace, replace(_run(), state="failed"), ())
    arguments = SimpleNamespace(deal_refs_command="status", run_id="run-1")
    assert deal_cli.run_crm_deal_refs(arguments, runtime) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] is False
    assert payload["progress"] == {"unsafe": True}


class OverflowRepository(Neo4jCrmDealRefsRepository):
    def __init__(self) -> None:
        self.deal_calls = 0
        self.identity_calls = 0

    def _read(self, query: str, parameters: object) -> list[dict[str, object]]:
        params = parameters
        assert isinstance(params, dict)
        limit = params["limit"]
        assert isinstance(limit, int)
        if "SourceRecord" in query:
            start = self.deal_calls * 10
            self.deal_calls += 1
            return [
                FakeRepository()._deal(str(start + index + 1), 1, f"pk-{start + index}")
                for index in range(limit)
            ]
        start = self.identity_calls * 10
        self.identity_calls += 1
        return [
            {
                "event_id": f"event-{start + index}",
                "global_revision": start + index + 1,
                "source_instance_id": "instance-a",
                "source_entity_id": "42",
                "identity_policy_version": "crm_deal_identity_v2",
                "link_status": "unresolved",
                "hyperp_person_id": None,
                "person_status": None,
                "resolution_kind": "baseline",
                "resolution_revision": start + index + 1,
                "effective_at": "2026-01-01T00:00:00Z",
                "created_at": "2026-01-02T00:00:00Z",
            }
            for index in range(limit)
        ]


def test_repository_max_plus_one_deal_and_identity_overflow_fails_early() -> None:
    repository = OverflowRepository()
    with pytest.raises(RuntimeError, match="deal references"):
        repository.list_deal_references("instance-a", "2026-02-01T00:00:00Z", 2, 2)
    assert repository.deal_calls == 2
    with pytest.raises(RuntimeError, match="identity revisions"):
        repository.list_identity_revisions("instance-a", ("42",), "2026-02-01T00:00:00Z", 99, 2, 2)
    assert repository.identity_calls == 2
