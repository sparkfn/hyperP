"""Adversarial State-backed operational quiescence evidence admission tests."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.config import RuntimeConfig
from intelligence.crm.activities.cleanup.quiescence import (
    admit_published_quiescence,
    quiescence_relative_path,
    registration_registry,
)
from intelligence.crm.activities.cleanup.types import QuiescenceEvidence, canonical_digest
from intelligence.models import OutputInventory, Run
from intelligence.runtime import IntelligenceRuntime


def _digest(value: str) -> str:
    return canonical_digest({"value": value})


def _evidence() -> QuiescenceEvidence:
    return QuiescenceEvidence.create(
        "quiescence-run",
        "archive-run",
        "checkpoint-a",
        "snapshot-a",
        _digest("manifest"),
        _digest("cleanup"),
        "bitrix-source",
        "bitrix-instance",
        "environment-a",
        "database-a",
        _digest("boundary"),
        "test-operator",
        "test-reference",
    )


class _State:
    def __init__(self, run: Run, outputs: tuple[OutputInventory, ...]) -> None:
        self.run = run
        self.outputs = outputs

    def inspect(self, run_id: str) -> Run | None:
        return self.run if run_id == self.run.run_id else None

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return self.outputs if run_id == self.run.run_id else ()


def _state_and_artifact(tmp_path: Path, evidence: QuiescenceEvidence) -> _State:
    relative = quiescence_relative_path(evidence.quiescence_run_id)
    path = tmp_path / "outputs" / evidence.quiescence_run_id / relative
    path.parent.mkdir(parents=True)
    raw = canonical_json(evidence.as_dict()).encode("utf-8")
    path.write_bytes(raw)
    return _State(
        Run(
            evidence.quiescence_run_id,
            "crm_activities_cleanup_quiescence",
            "completed",
            1,
            1.0,
            1.0,
        ),
        (
            OutputInventory(
                f"outputs/{evidence.quiescence_run_id}/{relative}",
                sha256(raw).hexdigest(),
                len(raw),
            ),
        ),
    )


def test_admits_one_exact_canonical_state_registered_quiescence_artifact(tmp_path: Path) -> None:
    evidence = _evidence()
    state = _state_and_artifact(tmp_path, evidence)
    assert (
        admit_published_quiescence(
            tmp_path, state, evidence.quiescence_run_id, evidence.evidence_digest
        )
        == evidence
    )


def test_registration_is_spawn_safe_and_uses_the_state_run_id(tmp_path: Path) -> None:
    template = _evidence()
    runtime = IntelligenceRuntime(
        RuntimeConfig(tmp_path, mutations_enabled=True), registration_registry(template)
    )
    try:
        run_id = runtime.run("crm_activities_cleanup_quiescence")
        relative = quiescence_relative_path(run_id)
        value = QuiescenceEvidence.parse(
            json.loads((tmp_path / "outputs" / run_id / relative).read_text())
        )
        assert value.quiescence_run_id == run_id
        assert value.cleanup_identity_digest == template.cleanup_identity_digest
        assert (
            admit_published_quiescence(tmp_path, runtime.state, run_id, value.evidence_digest)
            == value
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("mutation", ("sha", "bytes", "extra", "command", "state", "digest"))
def test_quiescence_admission_fails_closed_on_registration_or_digest_tampering(
    tmp_path: Path, mutation: str
) -> None:
    evidence = _evidence()
    state = _state_and_artifact(tmp_path, evidence)
    if mutation == "sha":
        state.outputs = (
            OutputInventory(
                state.outputs[0].relative_path, _digest("wrong"), state.outputs[0].byte_count
            ),
        )
    elif mutation == "bytes":
        state.outputs = (
            OutputInventory(state.outputs[0].relative_path, state.outputs[0].sha256, 1),
        )
    elif mutation == "extra":
        state.outputs = state.outputs + (
            OutputInventory("outputs/quiescence-run/other.json", _digest("other"), 1),
        )
    elif mutation == "command":
        state.run = Run(evidence.quiescence_run_id, "other", "completed", 1, 1.0, 1.0)
    elif mutation == "state":
        state.run = Run(
            evidence.quiescence_run_id, "crm_activities_cleanup_quiescence", "failed", 1, 1.0, 1.0
        )
    else:
        with pytest.raises(RuntimeError, match="binding"):
            admit_published_quiescence(
                tmp_path, state, evidence.quiescence_run_id, _digest("wrong")
            )
        return
    with pytest.raises((RuntimeError, ValueError)):
        admit_published_quiescence(
            tmp_path, state, evidence.quiescence_run_id, evidence.evidence_digest
        )


@pytest.mark.parametrize("field", ("writer_retired", "writers_quiescent", "source_key"))
def test_quiescence_payload_rejects_stale_or_non_operational_evidence(
    tmp_path: Path, field: str
) -> None:
    evidence = _evidence()
    state = _state_and_artifact(tmp_path, evidence)
    relative = quiescence_relative_path(evidence.quiescence_run_id)
    path = tmp_path / "outputs" / evidence.quiescence_run_id / relative
    value = evidence.as_dict()
    value[field] = False if field != "source_key" else ""
    path.write_bytes(canonical_json(value).encode("utf-8"))
    with pytest.raises((RuntimeError, ValueError)):
        admit_published_quiescence(
            tmp_path, state, evidence.quiescence_run_id, evidence.evidence_digest
        )


def test_quiescence_artifact_is_bounded_and_has_no_uri_fallback(tmp_path: Path) -> None:
    evidence = _evidence()
    state = _state_and_artifact(tmp_path, evidence)
    relative = quiescence_relative_path(evidence.quiescence_run_id)
    path = tmp_path / "outputs" / evidence.quiescence_run_id / relative
    path.write_bytes(b"x" * 2_000_001)
    with pytest.raises(RuntimeError, match="byte ceiling"):
        admit_published_quiescence(
            tmp_path, state, evidence.quiescence_run_id, evidence.evidence_digest
        )
    assert "uri" not in quiescence_relative_path(evidence.quiescence_run_id)
