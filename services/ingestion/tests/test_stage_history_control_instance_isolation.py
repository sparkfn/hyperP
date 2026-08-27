"""Control namespace invariants for stage-history worker coordination."""

from __future__ import annotations

from src.graph.queries.stage_history_ingestion import (
    CLAIM_STAGE_HISTORY_RETRY,
    CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
    COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
    CREATE_STAGE_HISTORY_UNIT,
    GET_STAGE_HISTORY_RECONCILIATION,
    GET_STAGE_HISTORY_STATUS,
    PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
    PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
    UPSERT_STAGE_HISTORY_OCCURRENCE,
    UPSERT_STAGE_HISTORY_RETRY,
)
from src.source_instances import scope_control_identity
from src.stage_history_task_lock import StageHistoryTaskLock


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> object:
        del ex
        if nx and name in self.values:
            return None
        self.values[name] = value
        return True

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> object:
        assert numkeys == 1
        key = str(keys_and_args[0])
        owner = str(keys_and_args[1])
        if self.values.get(key) != owner:
            return 0
        if "expire" in script:
            return 1
        del self.values[key]
        return 1


def test_stage_history_legacy_lock_key_and_owner_are_byte_compatible() -> None:
    client = _FakeRedis()
    lock = StageHistoryTaskLock(client, "artifact:task-1")

    assert lock.key == "profile_unifier:ingestion:source:bitrix_chat:crm_stage_history"
    assert lock.owner == "artifact:task-1"


def test_stage_history_nondefault_locks_do_not_collide() -> None:
    client = _FakeRedis()
    first = StageHistoryTaskLock(client, "artifact:task-1", control_instance_id="portal-one")
    second = StageHistoryTaskLock(client, "artifact:task-1", control_instance_id="portal-two")

    assert first.key == scope_control_identity(
        "profile_unifier:ingestion:source:bitrix_chat:crm_stage_history", "portal-one"
    )
    assert first.acquire() is True
    assert second.acquire() is True
    first.release()
    second.release()


def test_stage_history_queries_scope_every_control_plane_mutation() -> None:
    queries = (
        UPSERT_STAGE_HISTORY_OCCURRENCE,
        UPSERT_STAGE_HISTORY_RETRY,
        CLAIM_STAGE_HISTORY_RETRY,
        COMMIT_STAGE_HISTORY_UNIT_AND_ADVANCE_CHECKPOINT,
        PERSIST_STAGE_HISTORY_REVIEW_COMMAND,
        CLAIM_STAGE_HISTORY_REVIEW_COMMAND,
        PROJECT_STAGE_HISTORY_REVIEW_OUTCOME,
        GET_STAGE_HISTORY_STATUS,
        GET_STAGE_HISTORY_RECONCILIATION,
    )

    for query in queries:
        assert "$control_instance_id" in query


def test_stage_history_review_identity_is_control_scoped() -> None:
    assert "control_instance_id: $control_instance_id" in PERSIST_STAGE_HISTORY_REVIEW_COMMAND
    assert "control_instance_id: $control_instance_id" in CLAIM_STAGE_HISTORY_REVIEW_COMMAND
    assert "control_instance_id: $control_instance_id" in PROJECT_STAGE_HISTORY_REVIEW_OUTCOME


def test_stage_history_aggregate_children_are_explicitly_control_scoped() -> None:
    for query in (GET_STAGE_HISTORY_STATUS, GET_STAGE_HISTORY_RECONCILIATION):
        assert "StageHistoryUnit {control_instance_id: $control_instance_id}" in query
        assert "StageHistoryUnitAccounting {control_instance_id: $control_instance_id}" in query
    assert "StageHistoryOccurrence {control_instance_id: $control_instance_id}" in (
        GET_STAGE_HISTORY_RECONCILIATION
    )
    assert (
        "StageHistoryRetry {control_instance_id: $control_instance_id}"
        in GET_STAGE_HISTORY_RECONCILIATION
    )


def test_stage_history_publication_admission_is_defined_before_direct_execution_guard() -> None:
    from pathlib import Path

    source = Path("services/ingestion/src/stage_history_control.py").read_text()
    assert source.index("def _admit_stage_history_publication") < source.index(
        'if __name__ == "__main__":'
    )


def test_stage_history_generated_ids_preserve_legacy_and_scope_nondefault_controls() -> None:
    from src.stage_history_identities import scope_stage_history_identity

    raw_unit_id = "sha256:" + "b" * 64
    raw_occurrence_id = "sha256:" + "a" * 64

    assert scope_stage_history_identity(raw_unit_id, "legacy-default") == raw_unit_id
    assert scope_stage_history_identity(raw_occurrence_id, "legacy-default") == raw_occurrence_id
    assert scope_stage_history_identity(raw_unit_id, "portal-one") != scope_stage_history_identity(
        raw_unit_id, "portal-two"
    )
    assert scope_stage_history_identity(
        raw_occurrence_id, "portal-one"
    ) != scope_stage_history_identity(raw_occurrence_id, "portal-two")


def test_stage_history_pipeline_applies_scoped_ids_before_graph_persistence() -> None:
    from pathlib import Path

    source = Path("services/ingestion/src/stage_history_pipeline.py").read_text()
    assert "control_instance_id=fence.control_instance_id" in source
    assert "scope_stage_history_identity(" in source
    repository_source = Path("services/ingestion/src/graph/stage_history_ingestion.py").read_text()
    assert "retry_id = scope_stage_history_identity(" in repository_source


def test_stage_history_graph_merges_include_control_namespace_for_shared_artifacts() -> None:
    assert (
        "unit_id: $unit_id, control_instance_id: $control_instance_id" in CREATE_STAGE_HISTORY_UNIT
    )
    assert "occurrence_id: $occurrence_id, control_instance_id: $control_instance_id" in (
        UPSERT_STAGE_HISTORY_OCCURRENCE
    )
    retry_merge = (
        "MERGE (retry:StageHistoryRetry {\n"
        "  occurrence_id: $occurrence_id,\n"
        "  retry_sequence: $retry_sequence"
    )
    assert retry_merge in UPSERT_STAGE_HISTORY_RETRY
    assert "retry.control_instance_id = $control_instance_id" in UPSERT_STAGE_HISTORY_RETRY
