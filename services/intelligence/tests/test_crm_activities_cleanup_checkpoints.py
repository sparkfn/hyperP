"""Adversarial durable logical-cleanup checkpoint and reconciliation tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.reconciliation import parse_reconciliation, reconcile
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
)


def _digest(name: str) -> str:
    return canonical_digest({"name": name})


def _receipt(count: int = 2) -> CleanupReceipt:
    identities = tuple(
        CleanupIdentity(
            f"source-{index}",
            "crm_history",
            "bitrix-a",
            "v1",
            f"hash-{index}",
            _digest(f"record-{index}"),
            _digest(f"refs-{index}"),
            0,
            _digest(f"rels-{index}"),
            0,
            _digest(f"deps-{index}"),
        )
        for index in range(count)
    )
    return CleanupReceipt.create(
        "cleanup-a",
        CleanupAuthorization(
            "checkpoint-a",
            "archive-run",
            "snapshot-a",
            _digest("manifest"),
            _digest("boundary"),
            _digest("cleanup"),
            "archive-db",
        ),
        CleanupTarget("environment-a", "environment-a", "database-a", "database-a"),
        1,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        {"deals": 1},
        identities,
    )


def _checkpoint(
    tmp_path: Path, count: int = 2
) -> tuple[Path, CleanupReceipt, checkpoints.CleanupCheckpoint]:
    receipt = _receipt(count)
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-a")
    return root, receipt, checkpoints.initialize(root, "cleanup-a", receipt)


def test_checkpoint_persists_ordered_intent_result_and_resume_cursor(tmp_path: Path) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path)
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    checkpoint = checkpoints.write_batch_result(root, checkpoint, 1, {"source-0": "deleted"}, {})
    assert checkpoint.cursor == 1
    assert checkpoints.remaining_identities(root, checkpoint) == ("source-1",)
    with pytest.raises(RuntimeError, match="skips durable progress"):
        checkpoints.write_batch_intent(root, checkpoint, 3, ("source-1",))


def test_duplicate_identical_delivery_is_idempotent_but_conflict_fails(tmp_path: Path) -> None:
    root, _, checkpoint = _checkpoint(tmp_path, 1)
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    with pytest.raises(RuntimeError, match="outside receipt"):
        checkpoints.write_batch_intent(root, checkpoint, 1, ("other",))


def test_lost_acknowledgement_and_out_of_set_result_fail_closed(tmp_path: Path) -> None:
    root, _, checkpoint = _checkpoint(tmp_path)
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    assert checkpoints.unresolved_batch(root, checkpoint) == 1
    with pytest.raises(RuntimeError, match="uncertain"):
        checkpoints.remaining_identities(root, checkpoint)
    with pytest.raises(RuntimeError, match="out of set"):
        checkpoints.write_batch_result(root, checkpoint, 1, {"source-1": "deleted"}, {})


def test_checkpoint_binding_path_link_hardlink_and_corrupt_evidence_fail(tmp_path: Path) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path)
    with pytest.raises(RuntimeError, match="evidence conflicts"):
        checkpoints.initialize(root, "cleanup-a", _receipt(1))
    hardlink = root / "hardlink.json"
    try:
        os.link(root / "binding.json", hardlink)
    except OSError:
        pytest.skip("hardlinks unavailable")
    with pytest.raises(ValueError, match="unsafe"):
        checkpoints.load(root, "cleanup-a", receipt)
    hardlink.unlink()
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = root / "evil.json"
    try:
        link.symlink_to(target)
    except OSError:
        pass
    else:
        with pytest.raises(ValueError, match="unsafe"):
            checkpoints.load(root, "cleanup-a", receipt)
        link.unlink()
    (root / "checkpoint.json").write_text("{}", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        checkpoints.load(root, "cleanup-a", receipt)


def test_quota_and_reconciliation_balance_contracts(tmp_path: Path) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path, 1)
    (root / "large.json").write_bytes(b"x" * 100_001)
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, "cleanup-a", receipt)
    good = reconcile(
        receipt.logical_digest,
        ("source-0",),
        {"source-0": "already_absent"},
        {},
        {"records": 1},
        {"records": 0},
    )
    assert parse_reconciliation(good.as_dict(), ("source-0",)) == good
    bad = good.as_dict()
    bad["counts"] = {"deleted": 1, "already_absent": 0, "retained": 0, "conflict": 0, "failed": 0}
    with pytest.raises(ValueError, match="unbalanced"):
        parse_reconciliation(bad, ("source-0",))
    with pytest.raises(ValueError):
        parse_reconciliation({"authorized_count": 1}, ("source-0",))
