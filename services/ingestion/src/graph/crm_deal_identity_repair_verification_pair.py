"""Advisory reconciliation for only frozen CRM-repair pair-audit subjects."""

from __future__ import annotations

import json
from collections.abc import Mapping

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.verification_models import (
    RepairSecondaryAction,
    RepairSecondaryDispositionDetail,
    RepairVerificationCommand,
)
from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
from src.graph.crm_deal_identity_repair_verification_secondary import (
    SecondarySubjectError,
    frozen_pair_case_ids,
    secondary_detail,
)
from src.graph.crm_deal_identity_repair_verification_support import (
    json_value,
    required_record_int,
    required_row_string,
)
from src.graph.queries import crm_deal_identity_repair_verification as queries
from src.graph.queries import pair_audit_recalc as pair_queries
from src.matching.pair_score import score_person_pair
from src.models import JsonValue


def reconcile_pair_cases(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    person_ids: tuple[str, ...],
) -> list[RepairSecondaryDispositionDetail]:
    """Reconcile exactly the frozen pair cases; unrelated cases are never touched."""
    del person_ids
    try:
        expected_ids = frozen_pair_case_ids(command.inventory.payload)
    except SecondarySubjectError as exc:
        raise RepairVerificationDriftError("frozen pair closure differs") from exc
    rows = tuple(tx.run(queries.READ_REPAIR_PAIR_AUDIT_CASES, review_case_ids=list(expected_ids)))
    if tuple(required_row_string(row, "review_case_id") for row in rows) != expected_ids:
        raise RepairVerificationDriftError("current pair closure differs")
    return [_reconcile_pair_case(tx, command, row) for row in rows]


def _reconcile_pair_case(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    row: Record,
) -> RepairSecondaryDispositionDetail:
    case_id = required_row_string(row, "review_case_id")
    left_person_id = required_row_string(row, "left_person_id")
    right_person_id = required_row_string(row, "right_person_id")
    queue_state = required_row_string(row, "queue_state")
    supported = _bridge_supported(tx, left_person_id, right_person_id)
    expected: dict[str, JsonValue] = {"before": dict(_pair_state(row)), "supported": supported}
    action = _apply_pair_action(
        tx,
        case_id,
        left_person_id,
        right_person_id,
        queue_state,
        supported,
    )
    observed = _read_pair_state(tx, case_id)
    return secondary_detail(
        command,
        "pair_audit_case",
        case_id,
        action,
        "reconciled",
        expected=expected,
        observed={"after": dict(observed), "action": action},
    )


def _bridge_supported(tx: ManagedTransaction, left_person_id: str, right_person_id: str) -> bool:
    row = tx.run(
        queries.READ_PAIR_BRIDGE,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    return row is not None and required_record_int(row, "bridge_count") > 0


def _apply_pair_action(
    tx: ManagedTransaction,
    case_id: str,
    left_person_id: str,
    right_person_id: str,
    queue_state: str,
    supported: bool,
) -> RepairSecondaryAction:
    if not supported and queue_state == "open":
        row = tx.run(
            pair_queries.CANCEL_STALE_OPEN_PAIR_AUDIT_CASE,
            review_case_id=case_id,
        ).single()
        if row is None or required_row_string(row, "queue_state") != "resolved":
            raise RepairVerificationDriftError("stale pair cancellation CAS rejected")
        return "cancelled_stale_pair"
    if supported and queue_state in {"open", "assigned", "deferred"}:
        score = score_person_pair(tx, left_person_id, right_person_id)
        row = tx.run(
            pair_queries.UPDATE_PAIR_AUDIT_MATCH_DECISION,
            review_case_id=case_id,
            confidence=score.confidence,
            decision="review",
            reasons=score.reasons,
            feature_snapshot=json.dumps(
                {"heuristic_band": score.decision.value, **score.feature_snapshot},
                sort_keys=True,
            ),
            engine_version="pair_audit_repair_v1",
            policy_version="pair_audit_repair_v1",
        ).single()
        if row is None or required_row_string(row, "review_case_id") != case_id:
            raise RepairVerificationDriftError("pair rescore CAS rejected")
        return "rescored_pair"
    return "preserved"


def _read_pair_state(tx: ManagedTransaction, case_id: str) -> Mapping[str, JsonValue]:
    row = tx.run(queries.READ_PAIR_AUDIT_CASE_STATE, review_case_id=case_id).single()
    if row is None or required_row_string(row, "review_case_id") != case_id:
        raise RepairVerificationDriftError("pair post-state is missing")
    return _pair_state(row)


def _pair_state(row: Record) -> Mapping[str, JsonValue]:
    return {
        key: json_value(row[key])
        for key in (
            "review_case_id",
            "queue_state",
            "resolution",
            "confidence",
            "reasons",
            "feature_snapshot",
            "engine_version",
            "policy_version",
        )
        if key in row.keys()
    }


def read_pair_snapshot(
    tx: ManagedTransaction, command: RepairVerificationCommand
) -> tuple[Mapping[str, JsonValue], ...]:
    """Read exactly frozen pair states for the derived digest and replay checks."""
    try:
        expected_ids = frozen_pair_case_ids(command.inventory.payload)
    except SecondarySubjectError as exc:
        raise RepairVerificationDriftError("frozen pair closure differs") from exc
    rows = tuple(tx.run(queries.READ_REPAIR_PAIR_AUDIT_CASES, review_case_ids=list(expected_ids)))
    if tuple(required_row_string(row, "review_case_id") for row in rows) != expected_ids:
        raise RepairVerificationDriftError("current pair closure differs")
    return tuple(_pair_state(row) for row in rows)
