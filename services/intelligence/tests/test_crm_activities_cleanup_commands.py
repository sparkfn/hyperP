"""Focused orchestration contracts for manifest-gated activity cleanup."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.command_evidence import (
    _receipt as _admit_receipt,
)
from intelligence.crm.activities.cleanup.command_execution import (
    _after_prior_call_cleanup,
    _combine,
    _execute,
    _resolve_lost_ack,
    _verify,
)
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.reconciliation import reconcile
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.repositories.protocols.crm_activity_cleanup import (
    BatchOutcome,
    CleanupPlan,
    EndpointIdentity,
    ExactRecordInspection,
    ExpectedDeletionFact,
    IncidentRelationship,
    LiveTargetIdentity,
    ObservedRecord,
    ParentIdentity,
    RecordOutcome,
)


def _digest(value: str) -> str:
    return canonical_digest({"value": value})


def _identity(key: str) -> LiveTargetIdentity:
    return LiveTargetIdentity(
        key,
        "crm_history",
        "bitrix-a",
        "v1",
        f"hash-{key}",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
        "bitrix_chat",
    )


def _fact(key: str) -> ExpectedDeletionFact:
    identity = _identity(key)
    observed = ObservedRecord(
        f"node-{key}",
        ("SourceRecord",),
        "crm_history",
        "bitrix-a",
        "v1",
        f"hash-{key}",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
    )
    relationship = IncidentRelationship(
        f"relationship-{key}",
        "outbound",
        "FROM_SOURCE",
        EndpointIdentity(
            f"source-{key}",
            ("SourceSystem",),
            None,
            None,
            None,
            None,
            "bitrix_chat",
            None,
            None,
        ),
    )
    return ExpectedDeletionFact(
        identity, observed, (relationship,), (relationship.relationship_element_id,)
    )


def _receipt(
    keys: tuple[str, ...] = ("a", "b"),
    batch_size: int = 1,
    cleanup_run_id: str = "cleanup-a",
) -> CleanupReceipt:
    identities = tuple(
        CleanupIdentity(
            key,
            "crm_history",
            "bitrix-a",
            "v1",
            f"hash-{key}",
            _digest(f"record-{key}"),
            _digest(f"references-{key}"),
            0,
            _digest(f"relations-{key}"),
            0,
            _digest(f"dependencies-{key}"),
        )
        for key in keys
    )
    return CleanupReceipt.create(
        cleanup_run_id,
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
        batch_size,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        {"protected": 0},
        identities,
    )


def _ready_plan(keys: tuple[str, ...]) -> CleanupPlan:
    return CleanupPlan(
        tuple(_fact(key) for key in keys),
        (),
        tuple(RecordOutcome(key, "retained", "ready_for_batch_mutation") for key in keys),
    )


@dataclass
class _Repository:
    inspections: tuple[ExactRecordInspection, ...] = ()
    planned: CleanupPlan | None = None
    delete_calls: int = 0
    delete_outcome: BatchOutcome | None = None
    required_absence_calls: list[tuple[str, ...]] = field(default_factory=list)

    def database_identity(self) -> str:
        return "database-a"

    def inspect(self, _keys: tuple[str, ...]) -> tuple[ExactRecordInspection, ...]:
        if self.inspections:
            return self.inspections
        return tuple(_absent(key) for key in _keys)

    def plan(
        self,
        _identities: tuple[LiveTargetIdentity, ...],
        _inspections: tuple[ExactRecordInspection, ...],
    ) -> CleanupPlan:
        if self.planned is None:
            raise AssertionError("test did not provide a plan")
        return self.planned

    def delete_batch(
        self, identity: str, expected: tuple[ExpectedDeletionFact, ...]
    ) -> BatchOutcome:
        assert identity == "database-a"
        self.delete_calls += 1
        if self.delete_outcome is not None:
            return self.delete_outcome
        return BatchOutcome(
            identity,
            tuple(
                RecordOutcome(item.target.source_record_pk, "deleted", "deleted")
                for item in expected
            ),
            bool(expected),
        )

    def delete_batch_with_required_absences(
        self,
        identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
        required_absent: tuple[LiveTargetIdentity, ...],
    ) -> BatchOutcome:
        self.required_absence_calls.append(tuple(item.source_record_pk for item in required_absent))
        outcome = self.delete_batch(identity, expected)
        return BatchOutcome(
            identity,
            tuple(
                sorted(
                    (
                        *outcome.outcomes,
                        *(
                            RecordOutcome(
                                item.source_record_pk,
                                "already_absent",
                                "absent_at_mutation",
                            )
                            for item in required_absent
                        ),
                    ),
                    key=lambda item: item.source_record_pk,
                )
            ),
            outcome.mutation_applied,
        )

    def verify_protected(self, _protected: tuple[object, ...]) -> tuple[object, ...]:
        return ()

    def close(self) -> None:
        return None


def _absent(key: str) -> ExactRecordInspection:
    return ExactRecordInspection(key, 0, None, (), 0, False)


def test_receipt_is_deterministic_and_outcome_accounting_is_exact() -> None:
    assert _receipt().as_dict() == _receipt().as_dict()
    planned = {
        "a": RecordOutcome("a", "already_absent", "absent_before_mutation"),
        "b": RecordOutcome("b", "retained", "ready_for_batch_mutation"),
        "c": RecordOutcome("c", "conflict", "wrong_source_instance"),
    }
    outcome = BatchOutcome("database-a", (RecordOutcome("b", "deleted", "deleted"),), True)
    results, codes = _combine(("a", "b", "c"), planned, outcome)
    assert results == {"a": "already_absent", "b": "deleted", "c": "conflict"}
    assert codes == {"c": "wrong_source_instance"}


def test_activity_revalidation_allows_only_prior_authorized_call_edges_to_disappear() -> None:
    activity = _identity("activity-a")
    observed = ObservedRecord(
        "node-activity-a",
        ("SourceRecord",),
        "crm_history",
        "bitrix-a",
        "v1",
        "hash-activity-a",
        "active",
        "activity",
        ParentIdentity(None, None, None, None, None),
    )
    call_edge = IncidentRelationship(
        "call-edge",
        "inbound",
        "CHILD_OF",
        EndpointIdentity(
            "call-node", ("SourceRecord",), "call-a", None, None, None, None, None, None
        ),
    )
    unrelated = IncidentRelationship(
        "unrelated-edge",
        "inbound",
        "USES_IDENTIFIER",
        EndpointIdentity(
            "identifier", ("Identifier",), None, None, "phone", "+6500000000", None, None, None
        ),
    )
    source_edge = IncidentRelationship(
        "source-edge",
        "outbound",
        "FROM_SOURCE",
        EndpointIdentity(
            "source", ("SourceSystem",), None, None, None, None, "bitrix_chat", None, None
        ),
    )
    relationships = tuple(sorted((call_edge, source_edge, unrelated), key=IncidentRelationship.key))
    fact = ExpectedDeletionFact(activity, observed, relationships, ("source-edge",))
    adjusted = _after_prior_call_cleanup(fact, frozenset({"call-a"}))
    assert adjusted.incident_relationships == tuple(
        sorted((source_edge, unrelated), key=IncidentRelationship.key)
    )


def test_receipt_admission_rejects_exact_binding_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    receipt = _receipt()
    monkeypatch.setattr(
        "intelligence.crm.activities.cleanup.command_evidence.admit_published_receipt",
        lambda *_args: receipt,
    )

    class _State:
        workspace = Path(".")

    request = type("Request", (), {"batch_size": 1})()
    with pytest.raises(RuntimeError, match="does not bind"):
        _admit_receipt(
            _State(),
            "receipt-run",
            receipt.logical_digest,
            "cleanup-a",
            CleanupAuthorization(
                "other",
                "archive-run",
                "snapshot-a",
                _digest("manifest"),
                _digest("boundary"),
                _digest("cleanup"),
                "archive-db",
            ),
            receipt.target,
            request,
        )


def test_execute_progresses_batches_and_duplicate_delivery_is_idempotent(tmp_path: Path) -> None:
    receipt = _receipt()
    plan = _ready_plan(("a", "b"))
    repo = _Repository(planned=plan)
    _execute(
        receipt,
        "cleanup-a",
        tmp_path,
        repo,
        (_identity("a"), _identity("b")),
        plan,
        "attempt-one",
        lambda: False,
    )
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-a", create=False)
    checkpoint = checkpoints.load(root, "cleanup-a", receipt)
    assert checkpoint.phase == "reconciled"
    assert repo.delete_calls == 2
    assert repo.required_absence_calls == [("a",)]
    _execute(
        receipt,
        "cleanup-a",
        tmp_path,
        repo,
        (_identity("a"), _identity("b")),
        plan,
        "attempt-two",
        lambda: False,
    )
    assert repo.delete_calls == 2


def test_current_batch_absence_is_revalidated_with_ready_deletion(tmp_path: Path) -> None:
    receipt = _receipt(("a", "b"), 2, "cleanup-mixed-absence")
    plan = CleanupPlan(
        (_fact("a"),),
        (),
        (
            RecordOutcome("a", "retained", "ready_for_batch_mutation"),
            RecordOutcome("b", "already_absent", "absent_before_mutation"),
        ),
    )
    repo = _Repository(planned=plan)
    _execute(
        receipt,
        "cleanup-mixed-absence",
        tmp_path,
        repo,
        (_identity("a"), _identity("b")),
        plan,
        "attempt",
        lambda: False,
    )
    assert repo.required_absence_calls == [("b",)]
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-mixed-absence", create=False)
    checkpoint = checkpoints.load(root, "cleanup-mixed-absence", receipt)
    assert dict(checkpoints.durable_outcomes(root, checkpoint).outcomes) == {
        "a": "deleted",
        "b": "already_absent",
    }


def test_revalidation_conflict_rolls_back_batch_and_is_durably_accounted(tmp_path: Path) -> None:
    receipt = _receipt(("a",), 1, "cleanup-conflict")
    plan = _ready_plan(("a",))
    rolled_back = BatchOutcome(
        "database-a",
        (RecordOutcome("a", "conflict", "revalidation_mismatch"),),
        False,
    )
    repo = _Repository(planned=plan, delete_outcome=rolled_back)
    _execute(
        receipt,
        "cleanup-conflict",
        tmp_path,
        repo,
        (_identity("a"),),
        plan,
        "attempt",
        lambda: False,
    )
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-conflict", create=False)
    checkpoint = checkpoints.load(root, "cleanup-conflict", receipt)
    assert checkpoint.phase == "reconciled"
    assert (root / "batches" / "batch-00000001-result.json").read_text(encoding="utf-8").find(
        "conflict"
    ) >= 0


def test_preflight_conflict_rejects_the_entire_batch_before_delete(tmp_path: Path) -> None:
    receipt = _receipt(("a", "b"), 2, "cleanup-preflight")
    ready = _ready_plan(("a", "b"))
    plan = CleanupPlan(
        ready.expected_deletions,
        (),
        (
            RecordOutcome("a", "retained", "ready_for_batch_mutation"),
            RecordOutcome("b", "conflict", "wrong_source_instance"),
        ),
    )
    repo = _Repository(planned=plan)
    _execute(
        receipt,
        "cleanup-preflight",
        tmp_path,
        repo,
        (_identity("a"), _identity("b")),
        plan,
        "attempt",
        lambda: False,
    )
    assert repo.delete_calls == 0
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-preflight", create=False)
    evidence = checkpoints.durable_outcomes(
        root, checkpoints.load(root, "cleanup-preflight", receipt)
    )
    assert dict(evidence.outcomes) == {"a": "retained", "b": "conflict"}


def test_lost_acknowledgement_is_reinspected_without_claiming_deletion(tmp_path: Path) -> None:
    receipt = _receipt(("a",), 1, "cleanup-lost")
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-lost")
    checkpoint = checkpoints.initialize(root, "cleanup-lost", receipt)
    checkpoints.write_batch_intent(root, checkpoint, 1, ("a",))
    absent_plan = CleanupPlan(
        (), (), (RecordOutcome("a", "already_absent", "absent_after_intent"),)
    )
    repo = _Repository((_absent("a"),), absent_plan)
    checkpoint = _resolve_lost_ack(root, checkpoint, repo, (_identity("a"),))
    assert checkpoint.cursor == 1
    assert "already_absent" in (root / "batches" / "batch-00000001-result.json").read_text(
        encoding="utf-8"
    )
    assert repo.delete_calls == 0


def test_verify_is_read_only_and_rejects_failed_reconciliation(tmp_path: Path) -> None:
    receipt = _receipt(("a",), 1, "cleanup-verify")
    plan = _ready_plan(("a",))
    repo = _Repository(planned=plan)
    _execute(
        receipt, "cleanup-verify", tmp_path, repo, (_identity("a"),), plan, "attempt", lambda: False
    )
    repo.inspections = (_absent("a"),)
    staging = tmp_path / "staging" / "verify-attempt"
    staging.mkdir(parents=True)
    before = repo.delete_calls
    _verify(receipt, "cleanup-verify", tmp_path, repo, staging)
    assert repo.delete_calls == before
    assert list(staging.rglob("*.json"))
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-verify", create=False)
    failed = reconcile(
        receipt.logical_digest,
        ("a",),
        {"a": "failed"},
        {"a": "failed_after_execution"},
        {"protected": 0},
        {"protected": 0},
    )
    (root / "reconciliation.json").write_text(canonical_json(failed.as_dict()), encoding="utf-8")
    with pytest.raises((RuntimeError, ValueError)):
        _verify(receipt, "cleanup-verify", tmp_path, repo, staging)


def json_load(path: Path) -> dict[str, object]:
    import json

    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value
