"""Direct reproductions for the final formal dataset review findings."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.datasets import cli
from intelligence.datasets.admission import admit_activities
from intelligence.datasets.catalog import conflicts, entries
from intelligence.datasets.models import canonical_digest
from intelligence.models import OutputInventory, Run
from test_datasets_admission import _activity_run, _Config, _Runtime
from test_datasets_artifacts import _publish_dataset
from test_datasets_definition import _inputs


def test_activity_admission_accepts_offset_and_fractional_source_instants(tmp_path: Path) -> None:
    from intelligence.datasets.definition import compute

    inputs = _inputs(True)
    first = replace(
        inputs.activities.records[0],
        event_at="2026-01-01T06:00:00+00:00",
        observed_at="2026-01-01T06:00:00.123456789Z",
        ingested_at="2026-01-01T08:00:00+02:00",
        available_at="2026-01-01T06:00:00.123Z",
    )
    result = compute(replace(inputs, activities=replace(inputs.activities, records=(first,))))
    assert result.rows[0].archived_activity_count_lower_bound == 1


def test_activity_admission_rejects_false_boundary_and_cross_instance_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from intelligence.crm.activities.acceptance import accepted_publication

    state = __import__("intelligence.state", fromlist=["State"]).State(tmp_path)
    try:
        _run, _snapshot = _activity_run(state)
        runtime = _Runtime(_Config(tmp_path), state)
        descriptor, pointer = accepted_publication(runtime, "checkpoint-a") or (None, None)
        assert descriptor is not None and pointer is not None
        monkeypatch.setattr(
            "intelligence.datasets.admission.accepted_publication",
            lambda *_args: (replace(descriptor, boundary_digest="0" * 64), pointer),
        )
        with pytest.raises(ValueError, match="boundary"):
            admit_activities(runtime, "checkpoint-a")
    finally:
        state.close()


def test_catalog_missing_descriptor_evidence_fails_closed(tmp_path: Path) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    root = tmp_path / "outputs" / run_id / "acceptance-descriptors" / "datasets"
    (root / f"{descriptor.dataset_id}.json").unlink()
    with pytest.raises(ValueError):
        entries(tmp_path)
    with pytest.raises(ValueError):
        conflicts(tmp_path, descriptor.request_digest, "0" * 64)


def test_catalog_missing_descriptor_directory_fails_closed(tmp_path: Path) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    root = tmp_path / "outputs" / run_id / "acceptance-descriptors"
    for child in (root / "datasets").iterdir():
        child.unlink()
    (root / "datasets").rmdir()
    with pytest.raises(ValueError):
        entries(tmp_path)
    with pytest.raises(ValueError):
        conflicts(tmp_path, descriptor.request_digest, "0" * 64)


class _VerifyState:
    def __init__(self, run: Run, output: OutputInventory) -> None:
        self.run = run
        self.output = output

    def inspect(self, _run_id: str) -> Run:
        return self.run

    def accepted_outputs(self, _run_id: str) -> tuple[OutputInventory, ...]:
        return (self.output,)

    def close(self) -> None:
        return None


@pytest.mark.parametrize("state", ("timed_out", "cancelled", "failed"))
def test_verify_evidence_rejects_noncompleted_terminal_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    state: str,
) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    entry = __import__("intelligence.datasets.catalog", fromlist=["find"]).find(
        tmp_path, descriptor.dataset_id, run_id
    )
    verification = tmp_path / "outputs" / "verify-run" / "verifications" / "datasets"
    verification.mkdir(parents=True)
    path = verification / f"{descriptor.dataset_id}.json"
    value = {
        "accepted_dataset_run_id": descriptor.run_id,
        "dataset_id": descriptor.dataset_id,
        "descriptor_digest": descriptor.digest,
        "manifest_digest": descriptor.manifest_digest,
        "schema_version": "crm-deal-state-verification-v1",
        "verified": True,
    }
    value["digest"] = canonical_digest(value)
    raw = canonical_json(value).encode()
    path.write_bytes(raw)
    output = OutputInventory(
        f"outputs/verify-run/verifications/datasets/{descriptor.dataset_id}.json",
        __import__("hashlib").sha256(raw).hexdigest(),
        len(raw),
    )
    terminal = Run("verify-run", "dataset_verify", state, 1, 1.0, 1.0)
    state_module = __import__("intelligence.state_readonly", fromlist=["ReadOnlyState"])
    monkeypatch.setattr(
        state_module.ReadOnlyState,
        "open",
        classmethod(lambda _cls, _workspace: _VerifyState(terminal, output)),
    )
    with pytest.raises(RuntimeError, match="did not complete"):
        cli._verified_run_evidence(tmp_path, "verify-run", entry)


@pytest.mark.parametrize(
    "mutation",
    ("extra", "schema", "digest", "accepted_run", "dataset", "descriptor", "manifest"),
)
def test_verify_evidence_validates_exact_completed_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    entry = __import__("intelligence.datasets.catalog", fromlist=["find"]).find(
        tmp_path, descriptor.dataset_id, run_id
    )
    directory = tmp_path / "outputs" / "verify-run" / "verifications" / "datasets"
    directory.mkdir(parents=True)
    value = {
        "accepted_dataset_run_id": descriptor.run_id,
        "dataset_id": descriptor.dataset_id,
        "descriptor_digest": descriptor.digest,
        "manifest_digest": descriptor.manifest_digest,
        "schema_version": "crm-deal-state-verification-v1",
        "verified": True,
    }
    if mutation == "extra":
        value["extra"] = True
    if mutation == "schema":
        value["schema_version"] = "bad"
    if mutation == "accepted_run":
        value["accepted_dataset_run_id"] = "wrong"
    if mutation == "dataset":
        value["dataset_id"] = "wrong"
    if mutation == "descriptor":
        value["descriptor_digest"] = "0" * 64
    if mutation == "manifest":
        value["manifest_digest"] = "0" * 64
    value["digest"] = "0" * 64 if mutation == "digest" else canonical_digest(value)
    raw = canonical_json(value).encode()
    path = directory / f"{descriptor.dataset_id}.json"
    path.write_bytes(raw)
    output = OutputInventory(
        f"outputs/verify-run/verifications/datasets/{descriptor.dataset_id}.json",
        __import__("hashlib").sha256(raw).hexdigest(),
        len(raw),
    )
    state_module = __import__("intelligence.state_readonly", fromlist=["ReadOnlyState"])
    completed = Run("verify-run", "dataset_verify", "completed", 1, 1.0, 1.0)
    monkeypatch.setattr(
        state_module.ReadOnlyState,
        "open",
        classmethod(lambda _cls, _workspace: _VerifyState(completed, output)),
    )
    with pytest.raises(RuntimeError):
        cli._verified_run_evidence(tmp_path, "verify-run", entry)


def test_verify_evidence_accepts_exact_valid_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run_id, descriptor = _publish_dataset(tmp_path)
    entry = __import__("intelligence.datasets.catalog", fromlist=["find"]).find(
        tmp_path, descriptor.dataset_id, run_id
    )
    directory = tmp_path / "outputs" / "verify-run" / "verifications" / "datasets"
    directory.mkdir(parents=True)
    value = {
        "accepted_dataset_run_id": descriptor.run_id,
        "dataset_id": descriptor.dataset_id,
        "descriptor_digest": descriptor.digest,
        "manifest_digest": descriptor.manifest_digest,
        "schema_version": "crm-deal-state-verification-v1",
        "verified": True,
    }
    value["digest"] = canonical_digest(value)
    raw = canonical_json(value).encode()
    (directory / f"{descriptor.dataset_id}.json").write_bytes(raw)
    output = OutputInventory(
        f"outputs/verify-run/verifications/datasets/{descriptor.dataset_id}.json",
        __import__("hashlib").sha256(raw).hexdigest(),
        len(raw),
    )
    state_module = __import__("intelligence.state_readonly", fromlist=["ReadOnlyState"])
    monkeypatch.setattr(
        state_module.ReadOnlyState,
        "open",
        classmethod(
            lambda _cls, _workspace: _VerifyState(
                Run("verify-run", "dataset_verify", "completed", 1, 1.0, 1.0), output
            )
        ),
    )
    cli._verified_run_evidence(tmp_path, "verify-run", entry)
