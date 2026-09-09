"""Cross-module cleanup durable-evidence integration tests."""

from __future__ import annotations

from pathlib import Path

from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.status import read_status
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
)


def test_checkpoint_accessor_and_status_share_exact_outcome_evidence(tmp_path: Path) -> None:
    receipt = CleanupReceipt.create(
        "cleanup-a",
        CleanupAuthorization(
            "checkpoint-a",
            "archive-a",
            "snapshot-a",
            canonical_digest("manifest"),
            canonical_digest("boundary"),
            canonical_digest("cleanup"),
            "archive-db",
        ),
        CleanupTarget("environment-a", "environment-a", "database-a", "database-a"),
        1,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        {},
        (
            CleanupIdentity(
                "source-a",
                "crm_history",
                "bitrix-a",
                "v1",
                "hash-a",
                canonical_digest("record"),
                canonical_digest("reference"),
                0,
                canonical_digest("relationship"),
                0,
                canonical_digest("dependency"),
            ),
        ),
    )
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-a")
    checkpoint = checkpoints.initialize(root, "cleanup-a", receipt)
    checkpoints.record_attempt(root, checkpoint, "attempt-a")
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-a",))
    checkpoint = checkpoints.write_batch_result(
        root, checkpoint, 1, {"source-a": "conflict"}, {"source-a": "revalidation_mismatch"}
    )
    durable = checkpoints.durable_outcomes(root, checkpoint)
    assert durable.outcomes == (("source-a", "conflict"),)
    assert durable.failure_codes == (("source-a", "revalidation_mismatch"),)
    assert dict(read_status(tmp_path, "cleanup-a").outcome_counts)["conflict"] == 1
