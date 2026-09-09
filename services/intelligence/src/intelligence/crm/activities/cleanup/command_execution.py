"""Durable batch execution and verification for CRM activity cleanup."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup import checkpoints
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.reconciliation import reconcile
from intelligence.crm.activities.cleanup.types import canonical_digest
from intelligence.registry import Cancelled
from intelligence.repositories.protocols.crm_activity_cleanup import (
    BatchOutcome,
    CleanupPlan,
    CrmActivityCleanupRepository,
    ExactRecordInspection,
    ExpectedDeletionFact,
    LiveTargetIdentity,
    RecordOutcome,
    RequiredAbsenceCrmActivityCleanupRepository,
)

_READY = "ready_for_batch_mutation"


def _execute(
    receipt: CleanupReceipt,
    cleanup_run_id: str | None,
    workspace: Path,
    repository: RequiredAbsenceCrmActivityCleanupRepository,
    identities: tuple[LiveTargetIdentity, ...],
    plan: CleanupPlan,
    attempt_run_id: str,
    cancelled: Cancelled,
) -> None:
    run_id = _run_id(cleanup_run_id)
    root = checkpoints.checkpoint_root(workspace, run_id)
    checkpoint = (
        checkpoints.load(root, run_id, receipt)
        if (root / "binding.json").exists()
        else checkpoints.initialize(root, run_id, receipt)
    )
    checkpoints.record_attempt(root, checkpoint, attempt_run_id)
    checkpoint = checkpoints.recover_durable_results(root, checkpoint)
    _assert_processed_prefix_absent(root, checkpoint, repository)
    checkpoint = _resolve_lost_ack(root, checkpoint, repository, identities)
    if checkpoint.phase == "reconciled":
        _assert_processed_prefix_absent(root, checkpoint, repository)
        return
    expected = {item.target.source_record_pk: item for item in plan.expected_deletions}
    planned = {item.source_record_pk: item for item in plan.outcomes}
    while checkpoint.cursor < len(receipt.identities):
        if cancelled():
            raise RuntimeError("cleanup cancellation was requested")
        _assert_processed_prefix_absent(root, checkpoint, repository)
        checkpoint = _batch(
            root,
            checkpoint,
            repository,
            expected,
            planned,
            {item.source_record_pk: item for item in identities},
        )
    outcomes, codes = _checkpoint_outcomes(root, checkpoint)
    final_inspections = repository.inspect(
        tuple(sorted(item.source_record_pk for item in receipt.identities))
    )
    missing_protected = repository.verify_protected(receipt.protected_evidence)
    reconciliation = reconcile(
        checkpoint.receipt_digest,
        tuple(item.source_record_pk for item in receipt.identities),
        outcomes,
        codes,
        dict(receipt.protected_baseline),
        _after_counts(receipt, final_inspections, missing_protected),
    )
    checkpoints.write_reconciliation(root, checkpoint, reconciliation.as_dict())


def _assert_processed_prefix_absent(
    root: Path,
    checkpoint: checkpoints.CleanupCheckpoint,
    repository: CrmActivityCleanupRepository,
) -> None:
    """Fail closed if a previously deleted/absent identity reappears or drifts."""
    outcomes = dict(checkpoints.durable_outcomes(root, checkpoint).outcomes)
    keys = tuple(
        item.source_record_pk
        for item in checkpoint.receipt.identities[: checkpoint.cursor]
        if outcomes.get(item.source_record_pk) in {"deleted", "already_absent"}
    )
    if not keys:
        return
    inspected = repository.inspect(tuple(sorted(keys)))
    if any(item.matching_node_count != 0 for item in inspected):
        raise RuntimeError("processed cleanup identity reappeared or drifted")


def _resolve_lost_ack(
    root: Path,
    checkpoint: checkpoints.CleanupCheckpoint,
    repository: CrmActivityCleanupRepository,
    identities: tuple[LiveTargetIdentity, ...],
) -> checkpoints.CleanupCheckpoint:
    ordinal = checkpoints.unresolved_batch(root, checkpoint)
    if ordinal is None:
        return checkpoint
    batch = checkpoint.receipt.identities[
        checkpoint.cursor : checkpoint.cursor + checkpoint.receipt.batch_size
    ]
    identity_by_key = {item.source_record_pk: item for item in identities}
    selected = tuple(identity_by_key[item.source_record_pk] for item in batch)
    plan = repository.plan(
        selected, repository.inspect(tuple(item.source_record_pk for item in selected))
    )
    planned = {item.source_record_pk: item for item in plan.outcomes}
    outcomes: dict[str, str] = {}
    codes: dict[str, str] = {}
    for item in batch:
        result = planned[item.source_record_pk]
        if result.reason_code == _READY:
            outcomes[item.source_record_pk] = "failed"
            codes[item.source_record_pk] = "unacknowledged_intent_still_present"
        else:
            outcomes[item.source_record_pk] = result.classification
            if result.classification in {"retained", "conflict", "failed"}:
                codes[item.source_record_pk] = result.reason_code
    return checkpoints.write_batch_result(root, checkpoint, ordinal, outcomes, codes)


def _batch(
    root: Path,
    checkpoint: checkpoints.CleanupCheckpoint,
    repository: RequiredAbsenceCrmActivityCleanupRepository,
    expected: Mapping[str, ExpectedDeletionFact],
    planned: Mapping[str, RecordOutcome],
    identity_by_key: Mapping[str, LiveTargetIdentity],
) -> checkpoints.CleanupCheckpoint:
    ordinal = checkpoint.batch_count + 1
    batch = checkpoint.receipt.identities[
        checkpoint.cursor : checkpoint.cursor + checkpoint.receipt.batch_size
    ]
    keys = tuple(item.source_record_pk for item in batch)
    checkpoints.write_batch_intent(root, checkpoint, ordinal, keys)
    if any(
        planned[key].reason_code != _READY and planned[key].classification not in {"already_absent"}
        for key in keys
    ):
        outcomes, codes = _preflight_rejected(keys, planned)
        return checkpoints.write_batch_result(root, checkpoint, ordinal, outcomes, codes)
    successful_calls = _successful_prior_calls(root, checkpoint)
    facts = tuple(
        _after_prior_call_cleanup(expected[key], successful_calls)
        for key in keys
        if planned[key].reason_code == _READY
    )
    current_required_absent = tuple(
        identity_by_key[key] for key in keys if planned[key].classification == "already_absent"
    )
    durable_outcomes = dict(checkpoints.durable_outcomes(root, checkpoint).outcomes)
    prefix_required_absent = tuple(
        identity_by_key[item.source_record_pk]
        for item in checkpoint.receipt.identities[: checkpoint.cursor]
        if durable_outcomes.get(item.source_record_pk) in {"deleted", "already_absent"}
    )
    required_absent = tuple(
        sorted(
            {
                item.source_record_pk: item
                for item in (*prefix_required_absent, *current_required_absent)
            }.values(),
            key=lambda item: item.source_record_pk,
        )
    )
    database_identity = checkpoint.receipt.target.observed_database_identity
    if repository.database_identity() != database_identity:
        raise RuntimeError("live database identity changed before cleanup batch")
    returned = (
        repository.delete_batch(database_identity, facts)
        if not required_absent
        else repository.delete_batch_with_required_absences(
            database_identity,
            facts,
            tuple(sorted(required_absent, key=lambda item: item.source_record_pk)),
        )
    )
    if returned.database_identity != database_identity:
        raise RuntimeError("cleanup batch reported a different database identity")
    returned_by_id = {item.source_record_pk: item for item in returned.outcomes}
    guard_keys = {item.source_record_pk for item in prefix_required_absent}
    if any(
        returned_by_id.get(key) is None or returned_by_id[key].classification != "already_absent"
        for key in guard_keys
    ):
        raise RuntimeError("processed cleanup identity reappeared during mutation batch")
    current_outcome = BatchOutcome(
        returned.database_identity,
        tuple(item for item in returned.outcomes if item.source_record_pk in set(keys)),
        returned.mutation_applied,
    )
    outcomes, codes = _combine(keys, planned, current_outcome)
    return checkpoints.write_batch_result(root, checkpoint, ordinal, outcomes, codes)


def _preflight_rejected(
    keys: tuple[str, ...], planned: Mapping[str, RecordOutcome]
) -> tuple[dict[str, str], dict[str, str]]:
    outcomes: dict[str, str] = {}
    codes: dict[str, str] = {}
    for key in keys:
        item = planned[key]
        if item.reason_code == _READY:
            outcomes[key] = "retained"
            codes[key] = "batch_rejected_by_preflight"
        else:
            outcomes[key] = item.classification
            if item.classification in {"retained", "conflict", "failed"}:
                codes[key] = item.reason_code
    return outcomes, codes


def _successful_prior_calls(
    root: Path, checkpoint: checkpoints.CleanupCheckpoint
) -> frozenset[str]:
    evidence = checkpoints.durable_outcomes(root, checkpoint)
    outcomes = dict(evidence.outcomes)
    return frozenset(
        item.source_record_pk
        for item in checkpoint.receipt.identities[: checkpoint.cursor]
        if item.record_type == "call"
        and outcomes.get(item.source_record_pk) in {"deleted", "already_absent"}
    )


def _after_prior_call_cleanup(
    fact: ExpectedDeletionFact, successful_calls: frozenset[str]
) -> ExpectedDeletionFact:
    if fact.target.record_type != "crm_history" or not successful_calls:
        return fact
    relationships = tuple(
        item
        for item in fact.incident_relationships
        if not (
            item.direction == "inbound"
            and item.relationship_type in {"CHILD_OF", "DETAILS_HISTORY_ITEM"}
            and item.other_endpoint.source_record_pk in successful_calls
        )
    )
    return ExpectedDeletionFact(
        fact.target,
        fact.observed,
        relationships,
        fact.owned_relationship_element_ids,
    )


def _combine(
    keys: tuple[str, ...],
    planned: Mapping[str, RecordOutcome],
    batch: BatchOutcome,
) -> tuple[dict[str, str], dict[str, str]]:
    returned = {item.source_record_pk: item for item in batch.outcomes}
    outcomes: dict[str, str] = {}
    codes: dict[str, str] = {}
    for key in keys:
        initial, item = planned[key], returned.get(key)
        if initial.reason_code == _READY:
            if item is None:
                outcomes[key], codes[key] = "failed", "batch_outcome_missing"
            else:
                outcomes[key] = item.classification
                if item.classification in {"retained", "conflict", "failed"}:
                    codes[key] = item.reason_code
        elif initial.classification == "already_absent" and item is not None:
            if item.classification != "already_absent":
                outcomes[key] = item.classification
                codes[key] = item.reason_code
            else:
                outcomes[key] = "already_absent"
        elif item is not None:
            raise RuntimeError("batch returned an outcome for a non-mutation target")
        else:
            outcomes[key] = initial.classification
            if initial.classification in {"retained", "conflict", "failed"}:
                codes[key] = initial.reason_code
    return outcomes, codes


def _checkpoint_outcomes(
    root: Path,
    checkpoint: checkpoints.CleanupCheckpoint,
) -> tuple[dict[str, str], dict[str, str]]:
    evidence = checkpoints.durable_outcomes(root, checkpoint)
    return dict(evidence.outcomes), dict(evidence.failure_codes)


def _verify(
    receipt: CleanupReceipt,
    cleanup_run_id: str | None,
    workspace: Path,
    repository: CrmActivityCleanupRepository,
    staging: Path,
) -> None:
    run_id = _run_id(cleanup_run_id)
    root = checkpoints.checkpoint_root(workspace, run_id, create=False)
    checkpoint = checkpoints.load(root, run_id, receipt)
    if checkpoint.phase != "reconciled":
        raise RuntimeError("cleanup verification requires completed reconciliation")
    reconciliation = checkpoints.load_reconciliation(root, checkpoint)
    durable = checkpoints.durable_outcomes(root, checkpoint)
    from intelligence.crm.activities.cleanup.reconciliation import verify_durable_partition

    verify_durable_partition(
        reconciliation,
        receipt.logical_digest,
        durable.outcomes,
        durable.failure_codes,
    )
    if any(value not in {"deleted", "already_absent"} for _, value in reconciliation.outcomes):
        raise RuntimeError("cleanup verification rejects retained, conflict, or failed outcomes")
    inspections = repository.inspect(
        tuple(sorted(item.source_record_pk for item in receipt.identities))
    )
    if any(item.matching_node_count != 0 for item in inspections):
        raise RuntimeError(
            "cleanup verification found a selected identity still present or duplicated"
        )
    missing_protected = repository.verify_protected(receipt.protected_evidence)
    if missing_protected != ():
        raise RuntimeError("cleanup protected evidence changed")
    if dict(reconciliation.before_counts) != dict(receipt.protected_baseline) or dict(
        reconciliation.after_counts
    ) != _after_counts(receipt, inspections, missing_protected):
        raise RuntimeError("cleanup before/after reconciliation counts changed")
    evidence: dict[str, object] = {
        "schema_version": "crm-activities-cleanup-verification-v1",
        "receipt_digest": receipt.logical_digest,
        "cleanup_run_id": checkpoint.cleanup_run_id,
        "outcomes": dict(reconciliation.outcomes),
        "protected_baseline": dict(receipt.protected_baseline),
        "verified": True,
    }
    evidence["digest"] = canonical_digest(evidence)
    _write(staging, f"verifications/crm/activities/{staging.name}.json", evidence)


def _after_counts(
    receipt: CleanupReceipt,
    inspections: tuple[ExactRecordInspection, ...],
    missing_protected: tuple[object, ...],
) -> dict[str, int]:
    expected = tuple(sorted(item.source_record_pk for item in receipt.identities))
    if tuple(item.source_record_pk for item in inspections) != expected:
        raise RuntimeError("cleanup after-count inspection does not cover the receipt")
    return {
        "authorized_identity_count": len(receipt.identities),
        "present_identity_count": sum(item.matching_node_count == 1 for item in inspections),
        "absent_identity_count": sum(item.matching_node_count == 0 for item in inspections),
        "duplicate_identity_count": sum(item.matching_node_count > 1 for item in inspections),
        "incident_relationship_count": sum(
            item.incident_relationship_count for item in inspections
        ),
        "protected_evidence_count": len(receipt.protected_evidence) - len(missing_protected),
    }


def _run_id(value: str | None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(char in value for char in "/\\\x00")
    ):
        raise RuntimeError("cleanup run ID is invalid or missing")
    return value


def _write(staging: Path, relative: str, value: Mapping[str, object]) -> None:
    path = staging.joinpath(*relative.split("/"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(canonical_json(dict(value)).encode("utf-8"))


def _read(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError("cleanup checkpoint evidence is corrupt") from error
    if not isinstance(value, dict) or raw != canonical_json(value).encode("utf-8"):
        raise RuntimeError("cleanup checkpoint evidence is noncanonical")
    return dict(value)
