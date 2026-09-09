"""Adversarial receipt admission and deterministic logical receipt tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup.receipt import (
    CleanupReceipt,
    admit_published_receipt,
    receipt_relative_path,
)
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.models import OutputInventory, Run


def _digest(name: str) -> str:
    return canonical_digest({"name": name})


def _receipt() -> CleanupReceipt:
    authorization = CleanupAuthorization(
        "checkpoint-a",
        "archive-run",
        "snapshot-a",
        _digest("manifest"),
        _digest("boundary"),
        _digest("cleanup"),
        "archive-db-fingerprint",
    )
    target = CleanupTarget("test-environment", "test-environment", "neo4j-a", "neo4j-a")
    identity = CleanupIdentity(
        "source-a",
        "crm_history",
        "bitrix-a",
        "v1",
        "hash-a",
        _digest("record"),
        _digest("references"),
        2,
        _digest("relationships"),
        1,
        _digest("dependencies"),
    )
    return CleanupReceipt.create(
        "cleanup-a",
        authorization,
        target,
        10,
        ResourceCeilings(100_000, 100, 20, 100),
        "policy-v1",
        {"deals": 3, "persons": 4},
        (identity,),
    )


class _State:
    def __init__(self, run: Run, outputs: tuple[OutputInventory, ...]) -> None:
        self._run = run
        self._outputs = outputs

    def inspect(self, run_id: str) -> Run | None:
        return self._run if run_id == self._run.run_id else None

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return self._outputs if run_id == self._run.run_id else ()


def _state_and_artifact(tmp_path: Path, receipt: CleanupReceipt) -> tuple[_State, str]:
    run_id = "receipt-run"
    relative = receipt_relative_path(run_id)
    path = tmp_path / "outputs" / run_id / relative
    path.parent.mkdir(parents=True)
    raw = canonical_json(receipt.as_dict()).encode("utf-8")
    path.write_bytes(raw)
    run = Run(run_id, "crm_activities_cleanup_dry_run", "completed", 1, 1.0, 1.0)
    inventory = OutputInventory(f"outputs/{run_id}/{relative}", sha256(raw).hexdigest(), len(raw))
    return _State(run, (inventory,)), run_id


def test_receipt_is_deterministic_across_runtime_attempts() -> None:
    first = _receipt()
    second = _receipt()
    assert first.as_dict() == second.as_dict()
    assert "receipt-run" not in canonical_json(first.as_dict())


def test_receipt_orders_companion_calls_before_activities() -> None:
    receipt = _receipt()
    activity = receipt.identities[0]
    call = CleanupIdentity(
        "zz-call",
        "call",
        activity.source_instance_id,
        activity.source_record_version,
        activity.record_hash,
        activity.accepted_record_digest,
        activity.reference_fingerprint,
        activity.incident_relationship_count,
        activity.incident_relationship_digest,
        activity.dependency_count,
        activity.dependency_digest,
    )
    reordered = CleanupReceipt.create(
        receipt.cleanup_run_id,
        receipt.authorization,
        receipt.target,
        receipt.batch_size,
        receipt.resource_ceilings,
        receipt.policy_version,
        dict(receipt.protected_baseline),
        (activity, call),
    )
    assert [item.record_type for item in reordered.identities] == ["call", "crm_history"]


@pytest.mark.parametrize("mutation", ("sha", "bytes", "schema", "digest", "command", "extra"))
def test_state_registered_receipt_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    receipt = _receipt()
    state, run_id = _state_and_artifact(tmp_path, receipt)
    if mutation == "sha":
        state._outputs = (
            OutputInventory(
                state._outputs[0].relative_path, _digest("wrong"), state._outputs[0].byte_count
            ),
        )
    elif mutation == "bytes":
        state._outputs = (
            OutputInventory(state._outputs[0].relative_path, state._outputs[0].sha256, 1),
        )
    elif mutation == "schema":
        path = tmp_path / "outputs" / run_id / receipt_relative_path(run_id)
        value = receipt.as_dict()
        value["unexpected"] = True
        path.write_bytes(canonical_json(value).encode("utf-8"))
    elif mutation == "digest":
        with pytest.raises(RuntimeError):
            admit_published_receipt(tmp_path, state, run_id, _digest("wrong"))
        return
    elif mutation == "command":
        state._run = Run(run_id, "other", "completed", 1, 1.0, 1.0)
    else:
        state._outputs = state._outputs + (
            OutputInventory("outputs/receipt-run/other.json", _digest("other"), 1),
        )
    with pytest.raises((RuntimeError, ValueError)):
        admit_published_receipt(tmp_path, state, run_id, receipt.logical_digest)


def test_state_registered_receipt_admits_exact_canonical_artifact(tmp_path: Path) -> None:
    receipt = _receipt()
    state, run_id = _state_and_artifact(tmp_path, receipt)
    assert admit_published_receipt(tmp_path, state, run_id, receipt.logical_digest) == receipt
