"""Transaction-order and rollback tests for stage-history persistence."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Literal, TypeVar, cast

import pytest
from neo4j import ManagedTransaction, Record
from src.bitrix_ingestion_models import FenceContext
from src.connectors.bitrix_stage_history.canonical import (
    canonical_stage_hash_v1,
    encode_stage_source_record_id,
)
from src.connectors.bitrix_stage_history.models import StageHistoryItem
from src.graph.client import Neo4jClient
from src.graph.queries.crm_history_authority import (
    APPEND_CRM_HISTORY_AUTHORITY_DECISION,
)
from src.graph.queries.ingestion_control import LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
from src.graph.queries.stage_history_ingestion import (
    APPEND_STAGE_HISTORY_INVALIDATION_INTENTS,
    APPEND_STAGE_HISTORY_PARENT_DECISION,
    COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
    CREATE_STAGE_HISTORY_UNIT,
    GET_STAGE_HISTORY_AUTHORITY_HEAD,
    GET_STAGE_HISTORY_COMMITTED_UNIT,
    RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES,
    UPSERT_STAGE_HISTORY_OCCURRENCE,
    UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING,
    UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD,
)
from src.graph.stage_history_ingestion import (
    StageHistoryIngestionRepository,
    StageHistoryPersistenceError,
)
from src.stage_history_ingestion_models import (
    StageHistoryAccounting,
    StageHistoryAssociationAccounting,
    StageHistoryAuthorityAccounting,
    StageHistoryCheckpointSnapshot,
    StageHistoryIdentityAccounting,
    StageHistoryOccurrence,
    StageHistoryReplaySourceWindow,
    StageHistoryReplayUnit,
    StageHistoryRetryAccounting,
    StageHistoryTerminalAccounting,
    StageHistoryValidObservation,
    advance_stage_history_checkpoint,
)

T = TypeVar("T")
_NOW = datetime(2026, 8, 14, 8, 0, tzinfo=UTC)
_CONTRACT_ID = "12345678-1234-5678-9234-567812345678"
_DIGEST = "sha256:" + "a" * 64
_HMAC = "b" * 64


class _Result:
    def __init__(self, record: Record | None) -> None:
        self._record = record

    def single(self) -> Record | None:
        return self._record


class _Transaction:
    def __init__(self, responses: dict[str, list[Record | None]]) -> None:
        self.responses = responses
        self.queries: list[str] = []
        self.parameters: list[dict[str, object]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.queries.append(query)
        self.parameters.append(parameters)
        queue = self.responses.get(query)
        if queue is None or not queue:
            raise AssertionError("unexpected query in repository transaction")
        return _Result(queue.pop(0))


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self.transaction = transaction
        self.committed = False
        self.rolled_back = False

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        try:
            result = work(cast(ManagedTransaction, self.transaction))
        except Exception:
            self.rolled_back = True
            raise
        self.committed = True
        return result


def _record(**values: object) -> Record:
    return cast(Record, values)


def _fence() -> FenceContext:
    return FenceContext(
        logical_run_id="logical-1",
        ingest_run_id="attempt-1",
        source_key="bitrix_chat",
        stream_key="crm_stage_history",
        stream_generation=2,
        fencing_token=3,
        attempt_generation=1,
    )


def _checkpoint() -> StageHistoryCheckpointSnapshot:
    return StageHistoryCheckpointSnapshot(
        run_type="bounded_smoke_replay",
        source_window=StageHistoryReplaySourceWindow(
            stage_ingestion_artifact_id="artifact-1",
            artifact_manifest_hmac=_HMAC,
            source_contract_uuid=_CONTRACT_ID,
            entity_type_id="2",
            owner_artifact_id="owner-artifact-1",
            owner_manifest_digest=_HMAC,
            stage_artifact_id="stage-artifact-1",
            qualification_evidence_digest=_HMAC,
            canonical_hash_version="bitrix-stage-history-v1",
            traversal_contract="bounded_spool_reconcile",
            configuration_digest=_HMAC,
            limits_digest=_HMAC,
        ),
        last_page_sequence=None,
        revision=0,
        committed_unit_count=0,
        last_unit_id=None,
        last_unit_digest=None,
        accounting=StageHistoryAccounting(
            terminal=StageHistoryTerminalAccounting(),
            identity=StageHistoryIdentityAccounting(),
            association=StageHistoryAssociationAccounting(),
            authority=StageHistoryAuthorityAccounting(),
            retry=StageHistoryRetryAccounting(),
        ),
    )


def _unit() -> StageHistoryReplayUnit:
    item = StageHistoryItem(
        history_id="101",
        entity_type_id="2",
        owner_id="42",
        type_id="2",
        created_time=_NOW,
        created_time_source="CREATED_TIME",
        category_id="0",
        stage_semantic_id="P",
        stage_id="C0:NEW",
        raw_payload={"ID": "101", "OWNER_ID": "42"},
    )
    event_identity = encode_stage_source_record_id(_CONTRACT_ID, "2", "101")
    canonical_hash = canonical_stage_hash_v1(_CONTRACT_ID, item)
    observation = StageHistoryValidObservation(
        occurrence_id="occurrence-1",
        artifact_id="artifact-1",
        page_sequence=1,
        row_sequence=1,
        event_identity=event_identity,
        canonical_hash=canonical_hash,
        item=item,
        logical_parent_source_system="bitrix_chat",
        logical_parent_source_record_id="bitrix-crm-deal-42",
        source_observed_at=_NOW,
    )
    occurrence = StageHistoryOccurrence(
        observation=observation,
        disposition="canonical_effective",
        parse_scope="in_scope",
        identity_hash_state="new_variant",
        association_state="selected_active",
        authority_state="effective",
    )
    return StageHistoryReplayUnit(
        run_type="bounded_smoke_replay",
        unit_id="unit-1",
        artifact_id="artifact-1",
        page_sequence=1,
        page_digest=_DIGEST,
        occurrences=(occurrence,),
        accounting=StageHistoryAccounting.from_occurrences((occurrence,)),
    )


def _unit_with_identity_outcome(
    *,
    disposition: Literal["canonical_effective", "same_hash_replay", "differing_hash_conflict"],
    identity_hash_state: Literal["new_variant", "existing_same_hash", "new_conflict_variant"],
    authority_state: Literal["effective", "withheld_conflict"],
) -> StageHistoryReplayUnit:
    unit = _unit()
    occurrence = replace(
        unit.occurrences[0],
        disposition=disposition,
        identity_hash_state=identity_hash_state,
        authority_state=authority_state,
    )
    return replace(
        unit,
        occurrences=(occurrence,),
        accounting=StageHistoryAccounting.from_occurrences((occurrence,)),
    )


def _success_responses() -> dict[str, list[Record | None]]:
    return {
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
        GET_STAGE_HISTORY_COMMITTED_UNIT: [None],
        CREATE_STAGE_HISTORY_UNIT: [_record(unit_id="unit-1")],
        UPSERT_STAGE_HISTORY_OCCURRENCE: [_record(occurrence_id="occurrence-1")],
        UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD: [
            _record(
                source_record_pk="source-1",
                created=True,
                prior_different_variant_count=0,
            )
        ],
        RESOLVE_STAGE_HISTORY_PARENT_CANDIDATES: [
            _record(
                association_state="selected_active",
                selected_parent_source_record_pk="deal-source-1",
                active_count=1,
                pending_count=0,
            )
        ],
        APPEND_STAGE_HISTORY_PARENT_DECISION: [_record(decision_id="parent-1")],
        GET_STAGE_HISTORY_AUTHORITY_HEAD: [
            _record(
                head_version=0,
                authority_token=0,
                authority_state=None,
                decision_id=None,
                selected_variant_hash=None,
                selected_association_current=True,
                logical_parent_source_system=None,
                logical_parent_source_record_id=None,
            )
        ],
        APPEND_CRM_HISTORY_AUTHORITY_DECISION: [
            _record(
                decision_id="authority-1",
                head_version=1,
                authority_token=1,
                replayed=False,
                semantic_match=True,
            )
        ],
        APPEND_STAGE_HISTORY_INVALIDATION_INTENTS: [_record(intent_count=1)],
        UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING: [_record(unit_id="unit-1")],
        COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT: [_record(unit_id="unit-1", revision=1)],
    }


def test_persist_unit_orders_checkpoint_last_in_one_transaction() -> None:
    transaction = _Transaction(_success_responses())
    client = _Client(transaction)
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, client))

    result = repository.persist_unit(_unit(), _checkpoint(), _fence())

    assert result.outcome == "committed"
    assert result.checkpoint_after.revision == 1
    assert client.committed is True
    assert client.rolled_back is False
    assert transaction.queries[0] == LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE
    assert transaction.queries[-1] == COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT
    assert transaction.queries.index(UPSERT_STAGE_HISTORY_UNIT_ACCOUNTING) < (
        transaction.queries.index(COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT)
    )


@pytest.mark.parametrize(
    "failure_point",
    [
        "after_unit",
        "after_occurrence",
        "after_variant",
        "after_parent",
        "after_retry",
        "after_authority",
        "after_outbox",
        "after_accounting",
        "after_checkpoint",
    ],
)
def test_failure_injection_rolls_back_before_checkpoint(failure_point: str) -> None:
    transaction = _Transaction(_success_responses())
    client = _Client(transaction)

    def fail(point: str) -> None:
        if point == failure_point:
            raise RuntimeError("injected failure")

    repository = StageHistoryIngestionRepository(cast(Neo4jClient, client), failure_injector=fail)

    with pytest.raises(RuntimeError, match="injected failure"):
        repository.persist_unit(_unit(), _checkpoint(), _fence())

    assert client.rolled_back is True
    assert client.committed is False
    if failure_point == "after_checkpoint":
        assert transaction.queries[-1] == COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT
    else:
        assert COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT not in transaction.queries


def test_checkpoint_cas_failure_does_not_report_success() -> None:
    responses = _success_responses()
    responses[CREATE_STAGE_HISTORY_UNIT] = [None]
    transaction = _Transaction(responses)
    client = _Client(transaction)
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, client))

    with pytest.raises(StageHistoryPersistenceError, match="checkpoint CAS"):
        repository.persist_unit(_unit(), _checkpoint(), _fence())

    assert client.rolled_back is True
    assert UPSERT_STAGE_HISTORY_OCCURRENCE not in transaction.queries


def test_committed_tail_replay_is_a_noop_with_exact_checkpoint_evidence() -> None:
    unit = _unit()
    checkpoint = advance_stage_history_checkpoint(_checkpoint(), unit)
    cursor_json = '{"last_page_sequence":1,"revision":1}'
    transaction = _Transaction(
        {
            LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE: [_record(fence_lock_version=1)],
            GET_STAGE_HISTORY_COMMITTED_UNIT: [
                _record(
                    status="committed",
                    unit_digest=unit.page_digest,
                    next_cursor_json=cursor_json,
                    next_checkpoint_revision=1,
                    fetched_count=1,
                )
            ],
        }
    )
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, _Client(transaction)))

    result = repository.persist_unit(unit, checkpoint, _fence())

    assert result.outcome == "already_committed"
    assert result.checkpoint_before == result.checkpoint_after == checkpoint
    assert transaction.queries == [
        LOCK_AND_ASSERT_ACTIVE_BITRIX_FENCE,
        GET_STAGE_HISTORY_COMMITTED_UNIT,
    ]


@pytest.mark.parametrize(
    ("disposition", "identity_state", "authority_state", "created", "prior_count"),
    [
        ("canonical_effective", "new_variant", "effective", True, 0),
        ("same_hash_replay", "existing_same_hash", "effective", False, 1),
        (
            "differing_hash_conflict",
            "new_conflict_variant",
            "withheld_conflict",
            True,
            1,
        ),
    ],
)
def test_variant_identity_classification_is_derived_from_locked_graph_evidence(
    disposition: Literal["canonical_effective", "same_hash_replay", "differing_hash_conflict"],
    identity_state: Literal["new_variant", "existing_same_hash", "new_conflict_variant"],
    authority_state: Literal["effective", "withheld_conflict"],
    created: bool,
    prior_count: int,
) -> None:
    unit = _unit_with_identity_outcome(
        disposition=disposition,
        identity_hash_state=identity_state,
        authority_state=authority_state,
    )
    responses = _success_responses()
    responses[UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD] = [
        _record(
            source_record_pk="source-1",
            created=created,
            prior_different_variant_count=prior_count,
        )
    ]
    if disposition == "same_hash_replay":
        observation = unit.occurrences[0].observation
        assert isinstance(observation, StageHistoryValidObservation)
        responses[GET_STAGE_HISTORY_AUTHORITY_HEAD] = [
            _record(
                head_version=1,
                authority_token=1,
                authority_state="effective",
                decision_id="authority-0",
                selected_variant_hash=observation.canonical_hash,
                selected_association_current=True,
                logical_parent_source_system="bitrix_chat",
                logical_parent_source_record_id="bitrix-crm-deal-42",
            )
        ]
    transaction = _Transaction(responses)
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, _Client(transaction)))

    result = repository.persist_unit(unit, _checkpoint(), _fence())

    assert result.outcome == "committed"
    variant_parameters = transaction.parameters[
        transaction.queries.index(UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD)
    ]
    assert variant_parameters["required_run_type"] == "bounded_smoke_replay"
    if disposition == "differing_hash_conflict":
        outbox_parameters = transaction.parameters[
            transaction.queries.index(APPEND_STAGE_HISTORY_INVALIDATION_INTENTS)
        ]
        assert outbox_parameters["expected_intent_count"] == 0


def test_same_hash_replay_accepts_a_later_artifact_observation_timestamp() -> None:
    unit = _unit_with_identity_outcome(
        disposition="same_hash_replay",
        identity_hash_state="existing_same_hash",
        authority_state="effective",
    )
    original = unit.occurrences[0].observation
    assert isinstance(original, StageHistoryValidObservation)
    later_observation = replace(
        original,
        artifact_id="artifact-2",
        occurrence_id="occurrence-2",
        source_observed_at=_NOW + timedelta(hours=1),
    )
    later_occurrence = replace(unit.occurrences[0], observation=later_observation)
    later_unit = replace(
        unit,
        unit_id="unit-2",
        artifact_id="artifact-2",
        occurrences=(later_occurrence,),
        accounting=StageHistoryAccounting.from_occurrences((later_occurrence,)),
    )
    responses = _success_responses()
    responses[CREATE_STAGE_HISTORY_UNIT] = [_record(unit_id="unit-2")]
    responses[UPSERT_STAGE_HISTORY_OCCURRENCE] = [_record(occurrence_id="occurrence-2")]
    responses[UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD] = [
        _record(
            source_record_pk="source-1",
            created=False,
            prior_different_variant_count=1,
        )
    ]
    responses[GET_STAGE_HISTORY_AUTHORITY_HEAD] = [
        _record(
            head_version=1,
            authority_token=1,
            authority_state="effective",
            decision_id="authority-0",
            selected_variant_hash=later_observation.canonical_hash,
            selected_association_current=True,
            logical_parent_source_system="bitrix_chat",
            logical_parent_source_record_id="bitrix-crm-deal-42",
        )
    ]
    transaction = _Transaction(responses)
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, _Client(transaction)))
    checkpoint = _checkpoint()
    checkpoint = replace(
        checkpoint,
        source_window=replace(
            checkpoint.source_window,
            stage_ingestion_artifact_id="artifact-2",
        ),
    )

    result = repository.persist_unit(later_unit, checkpoint, _fence())

    assert result.outcome == "committed"
    variant_parameters = transaction.parameters[
        transaction.queries.index(UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD)
    ]
    assert (
        variant_parameters["source_observed_at"] == later_observation.source_observed_at.isoformat()
    )


def test_mismatched_caller_identity_classification_rolls_back() -> None:
    responses = _success_responses()
    responses[UPSERT_STAGE_HISTORY_VARIANT_SOURCE_RECORD] = [
        _record(
            source_record_pk="source-1",
            created=False,
            prior_different_variant_count=0,
        )
    ]
    transaction = _Transaction(responses)
    client = _Client(transaction)
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, client))

    with pytest.raises(StageHistoryPersistenceError, match="identity/hash state"):
        repository.persist_unit(_unit(), _checkpoint(), _fence())

    assert client.rolled_back is True
    assert APPEND_STAGE_HISTORY_PARENT_DECISION not in transaction.queries


def test_persist_unit_rejects_frozen_identity_or_hash_mismatch_before_graph_write() -> None:
    unit = _unit()
    occurrence = unit.occurrences[0]
    observation = occurrence.observation
    assert isinstance(observation, StageHistoryValidObservation)
    invalid_observation = replace(observation, event_identity="stage-event:forged")
    invalid_occurrence = replace(occurrence, observation=invalid_observation)
    invalid_unit = replace(
        unit,
        occurrences=(invalid_occurrence,),
        accounting=StageHistoryAccounting.from_occurrences((invalid_occurrence,)),
    )
    transaction = _Transaction({})
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, _Client(transaction)))

    with pytest.raises(ValueError, match="event identity"):
        repository.persist_unit(invalid_unit, _checkpoint(), _fence())

    assert transaction.queries == []


def test_persist_unit_rejects_an_entity_type_outside_the_frozen_source_window() -> None:
    unit = _unit()
    occurrence = unit.occurrences[0]
    observation = occurrence.observation
    assert isinstance(observation, StageHistoryValidObservation)
    changed_item = replace(observation.item, entity_type_id="3")
    changed_observation = replace(
        observation,
        event_identity=encode_stage_source_record_id(_CONTRACT_ID, "3", "101"),
        canonical_hash=canonical_stage_hash_v1(_CONTRACT_ID, changed_item),
        item=changed_item,
    )
    changed_occurrence = replace(occurrence, observation=changed_observation)
    changed_unit = replace(
        unit,
        occurrences=(changed_occurrence,),
        accounting=StageHistoryAccounting.from_occurrences((changed_occurrence,)),
    )
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, _Client(_Transaction({}))))

    with pytest.raises(ValueError, match="entity type"):
        repository.persist_unit(changed_unit, _checkpoint(), _fence())


def test_same_page_with_a_different_digest_is_not_treated_as_committed_replay() -> None:
    unit = _unit()
    checkpoint = advance_stage_history_checkpoint(_checkpoint(), unit)
    conflicting = replace(unit, page_digest="sha256:" + "f" * 64)
    repository = StageHistoryIngestionRepository(cast(Neo4jClient, _Client(_Transaction({}))))

    with pytest.raises(ValueError, match="contiguously"):
        repository.persist_unit(conflicting, checkpoint, _fence())
