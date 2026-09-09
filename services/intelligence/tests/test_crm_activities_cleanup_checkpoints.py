"""Adversarial durable logical-cleanup checkpoint and reconciliation tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.reconciliation import (
    parse_reconciliation,
    reconcile,
    verify_durable_partition,
)
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


def _mixed_receipt() -> CleanupReceipt:
    identities = (
        CleanupIdentity(
            "a-activity",
            "crm_history",
            "bitrix-a",
            "v1",
            "hash-activity",
            _digest("activity-record"),
            _digest("activity-refs"),
            0,
            _digest("activity-rels"),
            0,
            _digest("activity-deps"),
        ),
        CleanupIdentity(
            "z-call",
            "call",
            "bitrix-a",
            "v1",
            "hash-call",
            _digest("call-record"),
            _digest("call-refs"),
            0,
            _digest("call-rels"),
            0,
            _digest("call-deps"),
        ),
    )
    return CleanupReceipt.create(
        "cleanup-a",
        _receipt(1).authorization,
        _receipt(1).target,
        2,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        {"deals": 1},
        identities,
    )


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return dict(value)


def _write_canonical(path: Path, value: dict[str, object]) -> None:
    path.write_bytes(canonical_json(value).encode("utf-8"))


def _complete_one(
    root: Path, checkpoint: checkpoints.CleanupCheckpoint
) -> checkpoints.CleanupCheckpoint:
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    return checkpoints.write_batch_result(root, checkpoint, 1, {"source-0": "deleted"}, {})


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
    with pytest.raises(RuntimeError, match="conflicts"):
        checkpoints.write_batch_result(root, checkpoint, 1, {"source-1": "deleted"}, {})


def test_batch_parsers_reject_same_shape_tampering_and_pending_intent_tampering(
    tmp_path: Path,
) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path, 1)
    completed = _complete_one(root, checkpoint)
    result_path = root / "batches" / "batch-00000001-result.json"
    result = _read_json(result_path)

    result["receipt_digest"] = _digest("other-receipt")
    _write_canonical(result_path, result)
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, "cleanup-a", receipt)

    _write_canonical(
        result_path,
        checkpoints._result_dict(
            checkpoints.BatchResult(
                1,
                completed.receipt_digest,
                0,
                1,
                (("source-0", "deleted"),),
                (),
                canonical_digest({"outcomes": {"source-0": "deleted"}, "failure_codes": {}}),
            )
        ),
    )
    valid_result = _read_json(result_path)
    result = dict(valid_result)
    result["outcomes"] = {"source-0": "retained"}
    _write_canonical(result_path, result)
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, "cleanup-a", receipt)

    result = dict(valid_result)
    result["schema_version"] = "wrong-schema"
    _write_canonical(result_path, result)
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, "cleanup-a", receipt)

    pending_workspace = tmp_path / "pending"
    pending_workspace.mkdir()
    root, receipt, checkpoint = _checkpoint(pending_workspace, 2)
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    intent_path = root / "batches" / "batch-00000001-intent.json"
    intent = _read_json(intent_path)
    intent["identities"] = ["source-1"]
    intent["identity_digest"] = canonical_digest(["source-1"])
    _write_canonical(intent_path, intent)
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, "cleanup-a", receipt)


def test_batch_result_rejects_contradictory_failure_code(tmp_path: Path) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path, 1)
    checkpoint = _complete_one(root, checkpoint)
    result_path = root / "batches" / "batch-00000001-result.json"
    result = _read_json(result_path)
    result["failure_codes"] = {"source-0": "contradictory"}
    result["outcome_digest"] = canonical_digest(
        {"outcomes": result["outcomes"], "failure_codes": result["failure_codes"]}
    )
    _write_canonical(result_path, result)
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, checkpoint.cleanup_run_id, receipt)


def test_calls_first_schedule_survives_lexical_canonical_result_map_order(tmp_path: Path) -> None:
    receipt = _mixed_receipt()
    root = checkpoints.checkpoint_root(tmp_path, receipt.cleanup_run_id)
    checkpoint = checkpoints.initialize(root, receipt.cleanup_run_id, receipt)
    scheduled = tuple(item.source_record_pk for item in receipt.identities)
    assert scheduled == ("z-call", "a-activity")
    checkpoints.write_batch_intent(root, checkpoint, 1, scheduled)
    checkpoint = checkpoints.write_batch_result(
        root,
        checkpoint,
        1,
        {"a-activity": "deleted", "z-call": "deleted"},
        {},
    )
    raw = _read_json(root / "batches" / "batch-00000001-result.json")
    raw_outcomes = raw["outcomes"]
    assert isinstance(raw_outcomes, dict)
    assert list(raw_outcomes) == ["a-activity", "z-call"]
    evidence = checkpoints.durable_outcomes(root, checkpoint)
    assert evidence.outcomes == (("z-call", "deleted"), ("a-activity", "deleted"))
    assert checkpoints.remaining_identities(root, checkpoint) == ()


def test_result_ahead_of_cursor_recovers_once_from_original_durable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path, 1)
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-0",))
    replace_state = checkpoints._replace_state

    def fail_after_result(*_: object) -> None:
        raise RuntimeError("fault after durable result")

    monkeypatch.setattr(checkpoints, "_replace_state", fail_after_result)
    with pytest.raises(RuntimeError, match="fault after durable result"):
        checkpoints.write_batch_result(root, checkpoint, 1, {"source-0": "deleted"}, {})
    monkeypatch.setattr(checkpoints, "_replace_state", replace_state)

    stale = checkpoints.load(root, "cleanup-a", receipt)
    assert stale.cursor == 0
    recovered = checkpoints.recover_durable_results(root, stale)
    assert recovered.cursor == 1
    assert checkpoints.recover_durable_results(root, recovered) == recovered
    assert checkpoints.durable_outcomes(root, recovered).outcomes == (("source-0", "deleted"),)


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
    assert parse_reconciliation(good.as_dict(), ("source-0",), receipt.logical_digest) == good
    verify_durable_partition(
        good,
        receipt.logical_digest,
        (("source-0", "already_absent"),),
        (),
    )
    bad = good.as_dict()
    bad["counts"] = {"deleted": 1, "already_absent": 0, "retained": 0, "conflict": 0, "failed": 0}
    with pytest.raises(ValueError, match="unbalanced"):
        parse_reconciliation(bad, ("source-0",))
    with pytest.raises(ValueError):
        parse_reconciliation({"authorized_count": 1}, ("source-0",))


def test_reconciled_checkpoint_binds_receipt_and_complete_durable_partition(tmp_path: Path) -> None:
    root, receipt, checkpoint = _checkpoint(tmp_path, 1)
    checkpoint = _complete_one(root, checkpoint)
    good = reconcile(
        receipt.logical_digest,
        ("source-0",),
        {"source-0": "deleted"},
        {},
        {"records": 1},
        {"records": 0},
    )
    wrong_receipt = good.as_dict()
    wrong_receipt["receipt_digest"] = _digest("other-receipt")
    _write_canonical(root / "reconciliation.json", wrong_receipt)
    checkpoints._replace_state(
        root,
        receipt.logical_digest,
        checkpoint.cursor,
        checkpoint.batch_count,
        "reconciled",
    )
    with pytest.raises((RuntimeError, ValueError)):
        checkpoints.load(root, "cleanup-a", receipt)

    contradictory_workspace = tmp_path / "contradictory"
    contradictory_workspace.mkdir()
    root, receipt, checkpoint = _checkpoint(contradictory_workspace, 1)
    checkpoint = _complete_one(root, checkpoint)
    contradictory = good.as_dict()
    contradictory["receipt_digest"] = receipt.logical_digest
    contradictory["outcomes"] = {"source-0": "already_absent"}
    contradictory["counts"] = {
        "already_absent": 1,
        "conflict": 0,
        "deleted": 0,
        "failed": 0,
        "retained": 0,
    }
    contradictory["outcome_digest"] = canonical_digest(
        {"outcomes": contradictory["outcomes"], "failure_codes": contradictory["failure_codes"]}
    )
    _write_canonical(root / "reconciliation.json", contradictory)
    checkpoints._replace_state(
        root,
        receipt.logical_digest,
        checkpoint.cursor,
        checkpoint.batch_count,
        "reconciled",
    )
    with pytest.raises((RuntimeError, ValueError), match="durable|conflicts"):
        checkpoints.load(root, "cleanup-a", receipt)
