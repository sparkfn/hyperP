"""Read-only, bounded status for one durable logical cleanup checkpoint."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.reconciliation import parse_reconciliation
from intelligence.crm.activities.cleanup.types import DISPOSITIONS, require_identifier


@dataclass(frozen=True)
class CleanupStatus:
    cleanup_run_id: str
    receipt_digest: str
    phase: str
    cursor: int
    batch_count: int
    attempt_count: int
    outcome_counts: tuple[tuple[str, int], ...]
    reconciliation: tuple[tuple[str, int], ...] | None
    quiescence_evidence_digest: str
    protected_preservation_digest: str

    def as_dict(self) -> dict[str, object]:
        return {
            "cleanup_run_id": self.cleanup_run_id,
            "receipt_digest": self.receipt_digest,
            "phase": self.phase,
            "cursor": self.cursor,
            "batch_count": self.batch_count,
            "attempt_count": self.attempt_count,
            "outcome_counts": dict(self.outcome_counts),
            "reconciliation": None if self.reconciliation is None else dict(self.reconciliation),
            "quiescence_evidence_digest": self.quiescence_evidence_digest,
            "protected_preservation_digest": self.protected_preservation_digest,
        }


def read_status(workspace: Path, cleanup_run_id: str) -> CleanupStatus:
    """Read only canonical checkpoint evidence; no State, config, or graph access occurs."""
    run_id = require_identifier(cleanup_run_id, "cleanup_run_id")
    root = checkpoints.checkpoint_root(workspace, run_id, create=False)
    checkpoint = checkpoints.load_bound(root, run_id)
    evidence = checkpoints.durable_outcomes(root, checkpoint)
    counts = _counts(evidence.outcomes)
    return CleanupStatus(
        run_id,
        checkpoint.receipt_digest,
        checkpoint.phase,
        checkpoint.cursor,
        checkpoint.batch_count,
        len(evidence.attempts),
        counts,
        _reconciliation(root, checkpoint),
        checkpoint.receipt.quiescence_evidence_digest,
        checkpoint.receipt.protected_preservation.relationship_digest,
    )


def _counts(outcomes: tuple[tuple[str, str], ...]) -> tuple[tuple[str, int], ...]:
    return tuple(
        (name, sum(1 for _, value in outcomes if value == name)) for name in sorted(DISPOSITIONS)
    )


def _reconciliation(
    root: Path, checkpoint: checkpoints.CleanupCheckpoint
) -> tuple[tuple[str, int], ...] | None:
    if checkpoint.phase != "reconciled":
        return None
    raw = (root / "reconciliation.json").read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("cleanup reconciliation evidence is corrupt") from error
    if not isinstance(value, dict) or raw != canonical_json(value).encode("utf-8"):
        raise RuntimeError("cleanup reconciliation evidence is noncanonical")
    parsed = parse_reconciliation(
        value, tuple(item.source_record_pk for item in checkpoint.receipt.identities)
    )
    if parsed.receipt_digest != checkpoint.receipt_digest:
        raise RuntimeError("cleanup reconciliation receipt binding conflicts")
    return _counts(parsed.outcomes)
