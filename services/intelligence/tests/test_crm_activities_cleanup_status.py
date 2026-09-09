"""Read-only logical cleanup status tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.status import read_status
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ProtectedSourceEndpointEvidence,
    QuiescenceEvidence,
    ResourceCeilings,
    canonical_digest,
)


def _receipt() -> CleanupReceipt:
    identity = CleanupIdentity(
        "source-a",
        "crm_history",
        "bitrix-a",
        "v1",
        "hash-a",
        canonical_digest("record"),
        canonical_digest("refs"),
        0,
        canonical_digest("rels"),
        0,
        canonical_digest("deps"),
    )
    authorization = CleanupAuthorization(
        "checkpoint-a",
        "archive-a",
        "snapshot-a",
        canonical_digest("manifest"),
        canonical_digest("boundary"),
        canonical_digest("cleanup"),
        "archive-db",
    )
    target = CleanupTarget("environment-a", "environment-a", "database-a", "database-a")
    quiescence = QuiescenceEvidence.create(
        "quiescence-a",
        "archive-a",
        "checkpoint-a",
        "snapshot-a",
        canonical_digest("manifest"),
        canonical_digest("cleanup"),
        "bitrix_chat",
        "bitrix-a",
        "environment-a",
        "database-a",
        canonical_digest("boundary"),
    )
    endpoint = ProtectedSourceEndpointEvidence(
        "source-a",
        "relationship-a",
        "FROM_SOURCE",
        "outbound",
        "source-element-a",
        ("SourceSystem",),
        "bitrix_chat",
    )
    return CleanupReceipt.create(
        "cleanup-a",
        authorization,
        target,
        1,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        quiescence,
        {},
        (identity,),
        protected_source_endpoints=(endpoint,),
    )


def test_status_reads_only_validated_checkpoint_evidence(tmp_path: Path) -> None:
    receipt = _receipt()
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-a")
    checkpoint = checkpoints.initialize(root, "cleanup-a", receipt)
    checkpoints.record_attempt(root, checkpoint, "attempt-a")
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-a",))
    checkpoint = checkpoints.write_batch_result(
        root, checkpoint, 1, {"source-a": "already_absent"}, {}
    )
    value = {
        "schema_version": "crm-activities-cleanup-reconciliation-v2",
        "receipt_digest": receipt.logical_digest,
        "outcomes": {"source-a": "already_absent"},
        "failure_codes": {},
        "before_counts": {},
        "after_counts": {},
        "protected_preservation": receipt.protected_preservation.as_dict(),
        "counts": {"deleted": 0, "already_absent": 1, "retained": 0, "conflict": 0, "failed": 0},
        "authorized_count": 1,
        "unexplained_remainder": 0,
        "outcome_digest": canonical_digest(
            {"outcomes": {"source-a": "already_absent"}, "failure_codes": {}}
        ),
    }
    checkpoints.write_reconciliation(root, checkpoint, value)
    status = read_status(tmp_path, "cleanup-a")
    assert status.phase == "reconciled"
    assert status.attempt_count == 1
    assert dict(status.outcome_counts)["already_absent"] == 1


def test_status_rejects_tampered_checkpoint(tmp_path: Path) -> None:
    receipt = _receipt()
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-a")
    checkpoints.initialize(root, "cleanup-a", receipt)
    (root / "binding.json").write_text("{}", encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError)):
        read_status(tmp_path, "cleanup-a")
