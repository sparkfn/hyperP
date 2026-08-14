from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_stage_history.models import StageHistoryItem
from src.stage_history_ingestion_models import StageHistoryValidObservation
from src.stage_history_parent_lifecycle import (
    StageHistoryLifecycleSnapshot,
    build_lifecycle_occurrence,
)


def _observation() -> StageHistoryValidObservation:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    return StageHistoryValidObservation(
        occurrence_id="occurrence-1",
        artifact_id="artifact-1",
        page_sequence=1,
        row_sequence=1,
        event_identity="event-1",
        canonical_hash="sha256:" + "a" * 64,
        item=StageHistoryItem(
            history_id="1",
            entity_type_id="2",
            owner_id="42",
            type_id="2",
            created_time=now,
            created_time_source="2026-08-14T00:00:00Z",
            category_id="0",
            stage_semantic_id="P",
            stage_id="C0:NEW",
            raw_payload={"ID": "1", "OWNER_ID": "42"},
        ),
        logical_parent_source_system="bitrix_chat",
        logical_parent_source_record_id="bitrix-crm-deal-42",
        source_observed_at=now,
    )


@pytest.mark.parametrize(
    ("snapshot", "disposition", "retry"),
    (
        (
            StageHistoryLifecycleSnapshot("new_variant", "selected_active", "effective"),
            "canonical_effective",
            "none",
        ),
        (
            StageHistoryLifecycleSnapshot(
                "new_variant", "selected_pending_review", "withheld_parent"
            ),
            "canonical_pending_parent",
            "pending",
        ),
        (
            StageHistoryLifecycleSnapshot("new_variant", "waiting", "withheld_parent"),
            "parent_waiting",
            "pending",
        ),
        (
            StageHistoryLifecycleSnapshot("new_variant", "ambiguous", "withheld_conflict"),
            "parent_ambiguous",
            "pending",
        ),
        (
            StageHistoryLifecycleSnapshot("existing_same_hash", "selected_active", "effective"),
            "same_hash_replay",
            "none",
        ),
        (
            StageHistoryLifecycleSnapshot(
                "new_conflict_variant", "selected_active", "withheld_conflict"
            ),
            "differing_hash_conflict",
            "none",
        ),
    ),
)
def test_build_lifecycle_occurrence(
    snapshot: StageHistoryLifecycleSnapshot, disposition: str, retry: str
) -> None:
    occurrence = build_lifecycle_occurrence(_observation(), snapshot)
    assert occurrence.disposition == disposition
    assert occurrence.retry_state == retry


def test_build_lifecycle_occurrence_rejects_incoherent_snapshot() -> None:
    with pytest.raises(ValueError, match="not executable"):
        build_lifecycle_occurrence(
            _observation(),
            StageHistoryLifecycleSnapshot("new_variant", "selected_active", "withheld_parent"),
        )


def test_neo4j_reader_classifies_new_active_variant() -> None:
    from collections.abc import Callable
    from typing import TypeVar, cast

    from neo4j import ManagedTransaction, Record
    from src.graph.client import Neo4jClient
    from src.stage_history_parent_lifecycle import Neo4jStageHistoryLifecycleReader

    result_type = TypeVar("result_type")

    class Result:
        def single(self) -> Record:
            return cast(
                Record,
                {
                    "exact_count": 0,
                    "variant_count": 0,
                    "association_state": "selected_active",
                    "current_authority_state": None,
                },
            )

    class Transaction:
        def run(self, query: str, **parameters: object) -> Result:
            assert "CrmHistoryHashVariant" in query
            assert parameters["event_identity"] == "event-1"
            return Result()

    class Client:
        def execute_read(self, work: Callable[[ManagedTransaction], result_type]) -> result_type:
            return work(cast(ManagedTransaction, Transaction()))

    reader = Neo4jStageHistoryLifecycleReader(cast(Neo4jClient, Client()))
    assert reader.classify(_observation()) == StageHistoryLifecycleSnapshot(
        "new_variant", "selected_active", "effective"
    )


def test_neo4j_reader_rejects_known_variant_without_authority() -> None:
    from collections.abc import Callable
    from typing import TypeVar, cast

    from neo4j import ManagedTransaction, Record
    from src.graph.client import Neo4jClient
    from src.stage_history_parent_lifecycle import Neo4jStageHistoryLifecycleReader

    result_type = TypeVar("result_type")

    class Result:
        def single(self) -> Record:
            return cast(
                Record,
                {
                    "exact_count": 1,
                    "variant_count": 1,
                    "association_state": "selected_active",
                    "current_authority_state": None,
                },
            )

    class Transaction:
        def run(self, query: str, **parameters: object) -> Result:
            _ = query, parameters
            return Result()

    class Client:
        def execute_read(self, work: Callable[[ManagedTransaction], result_type]) -> result_type:
            return work(cast(ManagedTransaction, Transaction()))

    reader = Neo4jStageHistoryLifecycleReader(cast(Neo4jClient, Client()))
    with pytest.raises(RuntimeError, match="lacks an authority head"):
        reader.classify(_observation())
