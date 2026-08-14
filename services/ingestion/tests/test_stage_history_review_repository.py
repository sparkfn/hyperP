"""Atomic command-lifecycle tests for stage-history review mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
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
    GET_STAGE_HISTORY_REVIEW_OCCURRENCE,
    GET_STAGE_HISTORY_REVIEW_VARIANT_SET,
    LOCK_STAGE_HISTORY_REVIEW_EVENT,
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
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
        CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW: [_record(retry_sequence=1)],
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
            CLAIM_STAGE_HISTORY_RETRY_BY_REVIEW: [_record(retry_sequence=1)],
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


def test_reject_parent_with_one_candidate_persists_no_selected_parent() -> None:
    tx = _Tx(_review_success_responses())
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
