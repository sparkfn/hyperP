"""Atomic command-lifecycle tests for stage-history review mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from src.bitrix_ingestion_models import FenceContext
from src.graph.client import Neo4jClient
from src.graph.queries.crm_history_authority import (
    APPEND_CRM_HISTORY_AUTHORITY_DECISION,
    GET_CRM_HISTORY_AUTHORITY_HEAD,
)
from src.graph.queries.ingestion_control import LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
from src.graph.queries.stage_history_ingestion import (
    APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
    APPEND_STAGE_HISTORY_PARENT_DECISION,
    CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW,
    CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
    COMPLETE_STAGE_HISTORY_REVIEW_COMMAND,
    GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND,
    GET_STAGE_HISTORY_REVIEW_ASSOCIATION,
    GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT,
    GET_STAGE_HISTORY_REVIEW_OCCURRENCE,
    GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT,
    GET_STAGE_HISTORY_REVIEW_VARIANT_SET,
    LOCK_STAGE_HISTORY_REVIEW_EVENT,
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
    RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW,
    RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES,
)
from src.graph.stage_history_review import (
    StageHistoryReviewError,
    StageHistoryReviewRepository,
)
from src.stage_history_ingestion_models import StageHistoryReviewCommand

T = TypeVar("T")
_NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
_HASH = "sha256:" + "a" * 64
_VARIANT_SET_DIGEST = (
    "sha256:"
    + hashlib.sha256(
        json.dumps(
            ["stage-review-variant-set", _HASH],
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
)


def _result_digest() -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                [
                    "stage-review-result",
                    "command-1",
                    "authority-1",
                    "effective",
                    "2",
                    "2",
                    "1",
                ],
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
    )


class _Result:
    def __init__(self, record: Record | None) -> None:
        self.record = record

    def single(self) -> Record | None:
        return self.record


class _Tx:
    def __init__(self, responses: dict[str, list[Record | None]]) -> None:
        self.responses = responses
        self.queries: list[str] = []
        self.parameters: list[dict[str, object]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.queries.append(query)
        self.parameters.append(parameters)
        queue = self.responses.get(query)
        if queue is None or not queue:
            raise AssertionError("unexpected review query")
        return _Result(queue.pop(0))


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx
        self.committed = False
        self.rolled_back = False

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        try:
            result = work(cast(ManagedTransaction, self.tx))
        except Exception:
            self.rolled_back = True
            raise
        self.committed = True
        return result

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self.tx))


def _record(**values: object) -> Record:
    return cast(Record, values)


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="review-run-1",
        ingest_run_id="review-attempt-1",
        source_key="bitrix_chat",
        stream_key="crm_stage_history",
        stream_generation=4,
        fencing_token=5,
        attempt_generation=1,
    )


def _command(
    kind: Literal[
        "resolve_parent", "reject_parent", "resolve_conflict", "apply_correction"
    ] = "resolve_parent",
) -> StageHistoryReviewCommand:
    return StageHistoryReviewCommand(
        command_id="command-1",
        kind=kind,
        status="pending",
        event_identity="event-1",
        reviewer_id="reviewer-1",
        available_at=_NOW,
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state=(
            "withheld_parent"
            if kind in {"resolve_parent", "reject_parent"}
            else "withheld_conflict"
        ),
        expected_variant_set_digest=_VARIANT_SET_DIGEST,
        retry_sequence=1 if kind in {"resolve_parent", "reject_parent"} else None,
    )


def _review_success_responses() -> dict[str, list[Record | None]]:
    return {
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
        GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND: [None],
        CLAIM_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
        LOCK_STAGE_HISTORY_REVIEW_EVENT: [_record(lock_version=1)],
        GET_STAGE_HISTORY_REVIEW_OCCURRENCE: [
            _record(
                occurrence_id="occurrence-1",
                canonical_hash=_HASH,
                association_state="waiting",
                retry_state="pending",
                logical_parent_source_system="bitrix_chat",
                logical_parent_source_record_id="bitrix-crm-deal-42",
            )
        ],
        GET_CRM_HISTORY_AUTHORITY_HEAD: [
            _record(
                head_version=1,
                authority_token=1,
                authority_state="withheld_parent",
                logical_parent_source_system=None,
                logical_parent_source_record_id=None,
            )
        ],
        GET_STAGE_HISTORY_REVIEW_VARIANT_SET: [_record(canonical_hashes=[_HASH])],
        CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW: [
            _record(retry_sequence=1, attempt_count=1, max_attempts=5)
        ],
        RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES: [
            _record(
                logical_parent_source_system="bitrix_chat",
                logical_parent_source_record_id="bitrix-crm-deal-42",
                association_state="selected_active",
                selected_parent_source_record_pk="deal-source-1",
                active_count=1,
                pending_count=0,
            )
        ],
        APPEND_STAGE_HISTORY_PARENT_DECISION: [_record(decision_id="parent-1")],
        APPEND_CRM_HISTORY_AUTHORITY_DECISION: [
            _record(
                decision_id="authority-1",
                head_version=2,
                authority_token=2,
                replayed=False,
                semantic_match=True,
            )
        ],
        APPEND_STAGE_HISTORY_INVALIDATION_INTENTS: [_record(intent_count=1)],
        RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW: [_record(resolved_retry_count=1)],
        PROJECT_STAGE_HISTORY_REVIEW_OUTCOME: [
            _record(
                association_state="selected_active",
                authority_state="effective",
                retry_state="resolved",
                projected_occurrence_count=1,
            )
        ],
        COMPLETE_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
    }


def test_record_command_persists_provenance_before_execution() -> None:
    tx = _Tx(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            PERSIST_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    repository.record_command(
        _command(),
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        fence=_fence(),
    )

    assert tx.queries == [
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    ]
    assert tx.parameters[-1]["reviewer_actor"] == "reviewer-1"
    assert tx.parameters[-1]["authorization_reference"] == "authorization-1"


def test_resume_context_reuses_durable_command_time_and_provenance() -> None:
    command = _command()
    tx = _Tx(
        {
            GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT: [
                _record(
                    logical_run_id="review-run-1",
                    run_type="parent_reconcile",
                    logical_status="failed",
                    configuration_fingerprint="f" * 64,
                    worker_task_id="worker-1",
                    command_id=command.command_id,
                    review_kind=command.kind,
                    command_status="pending",
                    target_event_identity=command.event_identity,
                    target_occurrence_id="occurrence-1",
                    request_payload_digest=_command_payload_digest(command, "occurrence-1"),
                    reviewer_actor=command.reviewer_id,
                    authorization_reference="authorization-1",
                    available_at=command.available_at.isoformat(),
                    expected_head_version=command.expected_head_version,
                    expected_authority_token=command.expected_authority_token,
                    expected_authority_state=command.expected_authority_state,
                    expected_variant_set_digest=command.expected_variant_set_digest,
                    retry_sequence=command.retry_sequence,
                    selected_variant_hash=command.selected_variant_hash,
                    selected_association_decision_id=(command.selected_association_decision_id),
                    correction_of_decision_id=command.correction_of_decision_id,
                )
            ]
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    context = repository.load_resume_context("command-1")

    assert context is not None
    assert context.logical_run_id == "review-run-1"
    assert context.command.available_at == _NOW
    assert context.command == command
    assert context.occurrence_id == "occurrence-1"
    assert context.authorization_reference == "authorization-1"
    assert context.configuration_fingerprint == "f" * 64


def test_execution_context_loads_the_durable_configuration_fingerprint() -> None:
    command = _command()
    tx = _Tx(
        {
            GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT: [
                _record(
                    logical_run_id="review-run-1",
                    run_type="parent_reconcile",
                    logical_status="running",
                    configuration_fingerprint="f" * 64,
                    ingest_run_id="review-attempt-1",
                    worker_task_id="worker-1",
                    attempt_generation=1,
                    stream_generation=4,
                    fencing_token=5,
                    command_id=command.command_id,
                    review_kind=command.kind,
                    command_status="pending",
                    target_event_identity=command.event_identity,
                    target_occurrence_id="occurrence-1",
                    request_payload_digest=_command_payload_digest(command, "occurrence-1"),
                    reviewer_actor=command.reviewer_id,
                    authorization_reference="authorization-1",
                    available_at=command.available_at.isoformat(),
                    expected_head_version=command.expected_head_version,
                    expected_authority_token=command.expected_authority_token,
                    expected_authority_state=command.expected_authority_state,
                    expected_variant_set_digest=command.expected_variant_set_digest,
                    retry_sequence=command.retry_sequence,
                    selected_variant_hash=command.selected_variant_hash,
                    selected_association_decision_id=(command.selected_association_decision_id),
                    correction_of_decision_id=command.correction_of_decision_id,
                )
            ]
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    execution = repository.load_execution("command-1")

    assert execution is not None
    assert execution.command == command
    assert execution.configuration_fingerprint == "f" * 64
    assert execution.fence == _fence()


def test_record_command_revalidates_correction_before_durable_write() -> None:
    command = StageHistoryReviewCommand(
        command_id="command-1",
        kind="apply_correction",
        status="pending",
        event_identity="event-1",
        reviewer_id="reviewer-1",
        available_at=_NOW,
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state="withheld_conflict",
        expected_variant_set_digest=_VARIANT_SET_DIGEST,
        selected_variant_hash=_HASH,
        selected_association_decision_id="parent-1",
        correction_of_decision_id="authority-0",
    )
    object.__setattr__(command, "selected_association_decision_id", None)
    tx = _Tx({})
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    with pytest.raises(ValueError, match="selected association"):
        repository.record_command(
            command,
            occurrence_id="occurrence-1",
            authorization_reference="authorization-1",
            fence=_fence(),
        )

    assert tx.queries == []


def test_resolve_parent_claims_mutates_invalidates_and_completes_atomically() -> None:
    tx = _Tx(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND: [None],
            CLAIM_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
            LOCK_STAGE_HISTORY_REVIEW_EVENT: [_record(lock_version=1)],
            GET_STAGE_HISTORY_REVIEW_OCCURRENCE: [
                _record(
                    occurrence_id="occurrence-1",
                    canonical_hash=_HASH,
                    association_state="waiting",
                    retry_state="pending",
                    logical_parent_source_system="bitrix_chat",
                    logical_parent_source_record_id="bitrix-crm-deal-42",
                )
            ],
            RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES: [
                _record(
                    logical_parent_source_system="bitrix_chat",
                    logical_parent_source_record_id="bitrix-crm-deal-42",
                    association_state="selected_active",
                    selected_parent_source_record_pk="deal-source-1",
                    active_count=1,
                    pending_count=0,
                )
            ],
            APPEND_STAGE_HISTORY_PARENT_DECISION: [_record(decision_id="parent-1")],
            GET_CRM_HISTORY_AUTHORITY_HEAD: [
                _record(
                    head_version=1,
                    authority_token=1,
                    authority_state="withheld_parent",
                    logical_parent_source_system=None,
                    logical_parent_source_record_id=None,
                )
            ],
            GET_STAGE_HISTORY_REVIEW_VARIANT_SET: [_record(canonical_hashes=[_HASH])],
            CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW: [
                _record(retry_sequence=1, attempt_count=1, max_attempts=5)
            ],
            APPEND_CRM_HISTORY_AUTHORITY_DECISION: [
                _record(
                    decision_id="authority-1",
                    head_version=2,
                    authority_token=2,
                    replayed=False,
                    semantic_match=True,
                )
            ],
            APPEND_STAGE_HISTORY_INVALIDATION_INTENTS: [_record(intent_count=1)],
            RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW: [_record(resolved_retry_count=1)],
            PROJECT_STAGE_HISTORY_REVIEW_OUTCOME: [
                _record(
                    association_state="selected_active",
                    authority_state="effective",
                    retry_state="resolved",
                    projected_occurrence_count=1,
                )
            ],
            COMPLETE_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        _command(),
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        fence=_fence(),
    )

    assert result.authority_state == "effective"
    assert result.head_version == 2
    claim_parameters = tx.parameters[tx.queries.index(CLAIM_STAGE_HISTORY_REVIEW_COMMAND)]
    assert claim_parameters["required_run_type"] == "parent_reconcile"
    assert tx.queries[0] == LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    assert tx.queries[-1] == COMPLETE_STAGE_HISTORY_REVIEW_COMMAND
    assert tx.queries.index(APPEND_CRM_HISTORY_AUTHORITY_DECISION) < tx.queries.index(
        APPEND_STAGE_HISTORY_INVALIDATION_INTENTS
    )
    assert tx.queries.index(APPEND_STAGE_HISTORY_INVALIDATION_INTENTS) < tx.queries.index(
        COMPLETE_STAGE_HISTORY_REVIEW_COMMAND
    )
    retry_parameters = tx.parameters[tx.queries.index(RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW)]
    assert retry_parameters["retry_sequence"] == 1
    projection_parameters = tx.parameters[tx.queries.index(PROJECT_STAGE_HISTORY_REVIEW_OUTCOME)]
    assert projection_parameters["association_state"] == "selected_active"
    assert projection_parameters["authority_state"] == "effective"
    assert projection_parameters["retry_state"] == "resolved"


def test_reject_parent_with_one_candidate_persists_no_selected_parent() -> None:
    responses = _review_success_responses()
    responses[PROJECT_STAGE_HISTORY_REVIEW_OUTCOME] = [
        _record(
            association_state="rejected",
            authority_state="rejected",
            retry_state="rejected",
            projected_occurrence_count=1,
        )
    ]
    tx = _Tx(responses)
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        _command("reject_parent"),
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        fence=_fence(),
    )

    assert result.authority_state == "rejected"
    decision_parameters = tx.parameters[tx.queries.index(APPEND_STAGE_HISTORY_PARENT_DECISION)]
    assert decision_parameters["association_state"] == "rejected"
    assert decision_parameters["selected_parent_source_record_pk"] is None
    assert decision_parameters["active_candidate_count"] == 1
    projection_parameters = tx.parameters[tx.queries.index(PROJECT_STAGE_HISTORY_REVIEW_OUTCOME)]
    assert projection_parameters["association_state"] == "rejected"
    assert projection_parameters["authority_state"] == "rejected"
    assert projection_parameters["retry_state"] == "rejected"


def test_ambiguous_parent_authority_can_later_resolve_to_one_active_parent() -> None:
    responses = _review_success_responses()
    responses[GET_CRM_HISTORY_AUTHORITY_HEAD] = [
        _record(
            head_version=1,
            authority_token=1,
            authority_state="withheld_conflict",
            logical_parent_source_system=None,
            logical_parent_source_record_id=None,
        )
    ]
    command = replace(
        _command(),
        expected_authority_state="withheld_conflict",
    )
    tx = _Tx(responses)
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        command,
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        fence=_fence(),
    )

    assert result.authority_state == "effective"
    claim_parameters = tx.parameters[tx.queries.index(CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW)]
    assert claim_parameters["required_run_type"] == "parent_reconcile"


def test_unresolved_parent_review_reschedules_with_bounded_backoff() -> None:
    responses = _review_success_responses()
    responses[RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES] = [
        _record(
            logical_parent_source_system="bitrix_chat",
            logical_parent_source_record_id="bitrix-crm-deal-42",
            association_state="waiting",
            selected_parent_source_record_pk=None,
            active_count=0,
            pending_count=0,
        )
    ]
    responses[APPEND_STAGE_HISTORY_INVALIDATION_INTENTS] = [_record(intent_count=0)]
    responses[PROJECT_STAGE_HISTORY_REVIEW_OUTCOME] = [
        _record(
            association_state="waiting",
            authority_state="withheld_parent",
            retry_state="pending",
            projected_occurrence_count=1,
        )
    ]
    tx = _Tx(responses)
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        _command(),
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        retry_backoff_seconds=120,
        fence=_fence(),
    )

    assert result.authority_state == "withheld_parent"
    retry_parameters = tx.parameters[tx.queries.index(RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW)]
    assert retry_parameters["resolution"] == "pending"
    assert retry_parameters["next_attempt_at"] == (_NOW + timedelta(seconds=120)).isoformat()


def test_unresolved_parent_retry_is_quarantined_at_the_attempt_limit() -> None:
    responses = _review_success_responses()
    responses[GET_CRM_HISTORY_AUTHORITY_HEAD] = [
        _record(
            head_version=1,
            authority_token=1,
            authority_state="withheld_conflict",
            logical_parent_source_system=None,
            logical_parent_source_record_id=None,
        )
    ]
    responses[CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW] = [
        _record(retry_sequence=1, attempt_count=5, max_attempts=5)
    ]
    responses[RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES] = [
        _record(
            logical_parent_source_system="bitrix_chat",
            logical_parent_source_record_id="bitrix-crm-deal-42",
            association_state="ambiguous",
            selected_parent_source_record_pk=None,
            active_count=2,
            pending_count=0,
        )
    ]
    responses[APPEND_STAGE_HISTORY_INVALIDATION_INTENTS] = [_record(intent_count=0)]
    responses[PROJECT_STAGE_HISTORY_REVIEW_OUTCOME] = [
        _record(
            association_state="ambiguous",
            authority_state="withheld_conflict",
            retry_state="quarantined",
            projected_occurrence_count=1,
        )
    ]
    command = replace(
        _command(),
        expected_authority_state="withheld_conflict",
    )
    tx = _Tx(responses)
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        command,
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        fence=_fence(),
    )

    assert result.authority_state == "withheld_conflict"
    retry_parameters = tx.parameters[tx.queries.index(RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW)]
    assert retry_parameters["resolution"] == "quarantined"


@pytest.mark.parametrize(
    ("kind", "expected_state", "correction_target"),
    [
        ("resolve_conflict", "effective", None),
        ("apply_correction", "corrected", "authority-0"),
    ],
)
def test_variant_review_updates_current_projections_without_retry_mutation(
    kind: Literal["resolve_conflict", "apply_correction"],
    expected_state: Literal["effective", "corrected"],
    correction_target: str | None,
) -> None:
    command = StageHistoryReviewCommand(
        command_id="command-1",
        kind=kind,
        status="pending",
        event_identity="event-1",
        reviewer_id="reviewer-1",
        available_at=_NOW,
        expected_head_version=1,
        expected_authority_token=1,
        expected_authority_state=(
            "withheld_conflict" if kind == "resolve_conflict" else "effective"
        ),
        expected_variant_set_digest=_VARIANT_SET_DIGEST,
        selected_variant_hash=_HASH,
        selected_association_decision_id="parent-1",
        correction_of_decision_id=correction_target,
    )
    tx = _Tx(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND: [None],
            CLAIM_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
            LOCK_STAGE_HISTORY_REVIEW_EVENT: [_record(lock_version=1)],
            GET_STAGE_HISTORY_REVIEW_OCCURRENCE: [
                _record(
                    occurrence_id="occurrence-1",
                    canonical_hash=_HASH,
                    association_state="selected_active",
                    current_association_decision_id="parent-0",
                    retry_state="none",
                    logical_parent_source_system="bitrix_chat",
                    logical_parent_source_record_id="bitrix-crm-deal-42",
                )
            ],
            GET_CRM_HISTORY_AUTHORITY_HEAD: [
                _record(
                    head_version=1,
                    authority_token=1,
                    authority_state=command.expected_authority_state,
                    logical_parent_source_system="bitrix_chat",
                    logical_parent_source_record_id="bitrix-crm-deal-42",
                )
            ],
            GET_STAGE_HISTORY_REVIEW_VARIANT_SET: [_record(canonical_hashes=[_HASH])],
            GET_STAGE_HISTORY_REVIEW_ASSOCIATION: [
                _record(
                    decision_id="parent-1",
                    association_state="selected_active",
                    logical_parent_source_system="bitrix_chat",
                    logical_parent_source_record_id="bitrix-crm-deal-42",
                    selected_parent_source_record_pk="deal-source-1",
                )
            ],
            APPEND_CRM_HISTORY_AUTHORITY_DECISION: [
                _record(
                    decision_id="authority-1",
                    head_version=2,
                    authority_token=2,
                    replayed=False,
                    semantic_match=True,
                )
            ],
            APPEND_STAGE_HISTORY_INVALIDATION_INTENTS: [_record(intent_count=1)],
            PROJECT_STAGE_HISTORY_REVIEW_OUTCOME: [
                _record(
                    association_state="selected_active",
                    authority_state=expected_state,
                    retry_state="none",
                    projected_occurrence_count=2,
                )
            ],
            COMPLETE_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        command,
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        fence=_fence(),
    )

    assert result.authority_state == expected_state
    assert CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW not in tx.queries
    assert RESOLVE_STAGE_HISTORY_RETRY_BY_REVIEW not in tx.queries
    projection_parameters = tx.parameters[tx.queries.index(PROJECT_STAGE_HISTORY_REVIEW_OUTCOME)]
    assert projection_parameters["association_state"] == "selected_active"
    assert projection_parameters["association_decision_id"] == "parent-1"
    assert projection_parameters["authority_state"] == expected_state
    assert projection_parameters["retry_state"] is None


def test_review_command_rejects_a_stale_authority_head_before_domain_mutation() -> None:
    tx = _Tx(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND: [None],
            CLAIM_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
            LOCK_STAGE_HISTORY_REVIEW_EVENT: [_record(lock_version=1)],
            GET_STAGE_HISTORY_REVIEW_OCCURRENCE: [
                _record(
                    occurrence_id="occurrence-1",
                    canonical_hash=_HASH,
                    logical_parent_source_system="bitrix_chat",
                    logical_parent_source_record_id="bitrix-crm-deal-42",
                )
            ],
            GET_CRM_HISTORY_AUTHORITY_HEAD: [
                _record(
                    head_version=2,
                    authority_token=2,
                    authority_state="withheld_parent",
                    logical_parent_source_system=None,
                    logical_parent_source_record_id=None,
                )
            ],
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    with pytest.raises(StageHistoryReviewError, match="authority head is stale"):
        repository.execute_command(
            _command(),
            occurrence_id="occurrence-1",
            authorization_reference="authorization-1",
            lease_owner="worker-1",
            lease_expires_at=_NOW + timedelta(minutes=5),
            fence=_fence(),
        )

    assert RESOLVE_STAGE_HISTORY_REVIEW_PARENT_CANDIDATES not in tx.queries
    assert APPEND_CRM_HISTORY_AUTHORITY_DECISION not in tx.queries


def test_completed_review_redelivery_returns_the_durable_result_without_mutation() -> None:
    tx = _Tx(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND: [
                _record(
                    command_id="command-1",
                    authority_decision_id="authority-1",
                    authority_state="effective",
                    head_version=2,
                    authority_token=2,
                    invalidation_count=1,
                    result_digest=_result_digest(),
                )
            ],
        }
    )
    repository = StageHistoryReviewRepository(cast(Neo4jClient, _Client(tx)))

    result = repository.execute_command(
        _command(),
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        lease_owner="worker-1",
        lease_expires_at=_NOW + timedelta(minutes=5),
        fence=_fence(),
    )

    assert result.authority_decision_id == "authority-1"
    assert result.head_version == 2
    assert tx.queries == [
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        GET_COMPLETED_STAGE_HISTORY_REVIEW_COMMAND,
    ]


@pytest.mark.parametrize(
    "failure_point",
    [
        "after_claim",
        "after_event_lock",
        "after_retry_claim",
        "after_parent",
        "after_authority",
        "after_outbox",
        "after_retry",
        "after_projection",
        "after_completion",
    ],
)
def test_review_failure_injection_rolls_back_every_mutation_family(
    failure_point: str,
) -> None:
    tx = _Tx(_review_success_responses())
    client = _Client(tx)

    def fail(point: str) -> None:
        if point == failure_point:
            raise RuntimeError("injected review failure")

    repository = StageHistoryReviewRepository(
        cast(Neo4jClient, client),
        failure_injector=fail,
    )

    with pytest.raises(RuntimeError, match="injected review failure"):
        repository.execute_command(
            _command(),
            occurrence_id="occurrence-1",
            authorization_reference="authorization-1",
            lease_owner="worker-1",
            lease_expires_at=_NOW + timedelta(minutes=5),
            fence=_fence(),
        )

    assert client.rolled_back is True
    assert client.committed is False
    if failure_point == "after_completion":
        assert tx.queries[-1] == COMPLETE_STAGE_HISTORY_REVIEW_COMMAND


def _command_payload_digest(command: StageHistoryReviewCommand, occurrence_id: str) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                [
                    "stage-review-command",
                    command.command_id,
                    command.kind,
                    command.event_identity,
                    occurrence_id,
                    command.reviewer_id,
                    command.available_at.isoformat(),
                    str(command.expected_head_version),
                    str(command.expected_authority_token),
                    command.expected_authority_state,
                    command.expected_variant_set_digest,
                    str(command.retry_sequence or 0),
                    command.selected_variant_hash or "",
                    command.selected_association_decision_id or "",
                    command.correction_of_decision_id or "",
                ],
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
    )


def test_review_command_identity_scopes_nondefault_controls_and_preserves_legacy_values() -> None:
    legacy_tx = _Tx(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            PERSIST_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
        }
    )
    StageHistoryReviewRepository(cast(Neo4jClient, _Client(legacy_tx))).record_command(
        _command(),
        occurrence_id="occurrence-1",
        authorization_reference="authorization-1",
        fence=_fence(),
    )
    legacy_params = legacy_tx.parameters[-1]

    scoped_command_ids: list[str] = []
    scoped_occurrence_ids: list[str] = []
    for control_instance_id in ("portal-one", "portal-two"):
        tx = _Tx(
            {
                LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
                PERSIST_STAGE_HISTORY_REVIEW_COMMAND: [_record(command_id="command-1")],
                GET_STAGE_HISTORY_REVIEW_COMMAND_CONTEXT: [None],
                GET_STAGE_HISTORY_REVIEW_RESUME_CONTEXT: [None],
            }
        )
        repository = StageHistoryReviewRepository(
            cast(Neo4jClient, _Client(tx)), control_instance_id=control_instance_id
        )
        fence = replace(_fence(), control_instance_id=control_instance_id)
        repository.record_command(
            _command(),
            occurrence_id="occurrence-1",
            authorization_reference="authorization-1",
            fence=fence,
        )
        repository.load_execution("command-1")
        repository.load_resume_context("command-1")
        persist_params = tx.parameters[1]
        scoped_command_ids.append(cast(str, persist_params["command_id"]))
        scoped_occurrence_ids.append(cast(str, persist_params["target_occurrence_id"]))
        assert tx.parameters[2]["command_id"] == persist_params["command_id"]
        assert tx.parameters[3]["command_id"] == persist_params["command_id"]

    assert legacy_params["command_id"] == "command-1"
    assert legacy_params["target_occurrence_id"] == "occurrence-1"
    assert scoped_command_ids[0] != scoped_command_ids[1]
    assert scoped_occurrence_ids[0] != scoped_occurrence_ids[1]
