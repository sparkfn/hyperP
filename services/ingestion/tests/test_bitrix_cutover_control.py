"""Cutover state transitions preserve frozen/rejected generation safety."""

import json
from pathlib import Path

from src.bitrix_backfill_control import CONTROL_COMMANDS, load_qualification
from src.graph.queries.bitrix_backfill import (
    ACTIVATE_BITRIX_SUCCESSOR_GENERATION,
    ALLOCATE_BITRIX_BACKFILL_GENERATION,
    ALLOCATE_BITRIX_SUCCESSOR_GENERATION,
    CAS_BITRIX_BACKFILL_GENERATION_STATUS,
    CONFIRM_BITRIX_SUCCESSOR_PUBLICATION,
    RECORD_BITRIX_QUALIFICATION,
    REJECT_BITRIX_BACKFILL_GENERATION,
)
from src.graph.queries.ingestion_control import CREATE_LOGICAL_RUN_AND_ATTEMPT
from src.graph.queries.source_records import (
    CREATE_INGEST_RUN,
    CREATE_OR_REUSE_WORKER_INGEST_RUN,
)


def test_cli_exposes_the_complete_operator_workflow() -> None:
    assert CONTROL_COMMANDS == {
        "inventory",
        "allocate",
        "start",
        "status",
        "request-stop",
        "resume",
        "reconcile",
        "freeze",
        "qualify",
        "accept",
        "reject",
        "activate",
        "verify-tail",
        "rollback-status",
    }


def test_acceptance_and_activation_use_distinct_cas_bound_generations() -> None:
    assert "generation.status IN $expected_statuses" in CAS_BITRIX_BACKFILL_GENERATION_STATUS
    assert "generation.repository_sha = $repository_sha" in (CAS_BITRIX_BACKFILL_GENERATION_STATUS)
    assert "generation.status = 'frozen'" in RECORD_BITRIX_QUALIFICATION
    assert "successor.status = 'allocated'" in ALLOCATE_BITRIX_SUCCESSOR_GENERATION
    assert "corrective.status = 'accepted'" in ACTIVATE_BITRIX_SUCCESSOR_GENERATION
    assert "successor.status = 'activating'" in ACTIVATE_BITRIX_SUCCESSOR_GENERATION
    assert "successor.status = 'active'" in CONFIRM_BITRIX_SUCCESSOR_PUBLICATION
    assert "outbox.status = 'published'" in CONFIRM_BITRIX_SUCCESSOR_PUBLICATION


def test_generation_allocation_preserves_scope_after_creation_token_removal() -> None:
    assert (
        "REMOVE generation.creation_token\nWITH generation, created\nWHERE"
        in ALLOCATE_BITRIX_BACKFILL_GENERATION
    )
    assert (
        "REMOVE successor.creation_token\nWITH corrective, successor, created\nWHERE"
        in ALLOCATE_BITRIX_SUCCESSOR_GENERATION
    )


def test_rejection_is_terminal_and_blocks_all_bitrix_dispatch() -> None:
    assert "generation.status IN" in REJECT_BITRIX_BACKFILL_GENERATION
    assert "generation.status = 'rejected'" in REJECT_BITRIX_BACKFILL_GENERATION
    assert "dispatch.blocked = true" in REJECT_BITRIX_BACKFILL_GENERATION
    assert "stream.status = 'superseded'" in REJECT_BITRIX_BACKFILL_GENERATION
    for query in (
        CREATE_LOGICAL_RUN_AND_ATTEMPT,
        CREATE_INGEST_RUN,
        CREATE_OR_REUSE_WORKER_INGEST_RUN,
    ):
        assert "BitrixDispatchControl" in query
        assert "coalesce(dispatch.blocked, false) = false" in query


def test_qualification_loader_accepts_the_source_free_capability_result(
    tmp_path: Path,
) -> None:
    evidence = tmp_path / "qualification.json"
    evidence.write_text(
        json.dumps(
            {
                "owner_artifact_id": "owner-1",
                "stage_artifact_id": "stage-1",
                "owner_recommendation": "verified_keyset",
                "stage_recommendation": "bounded_spool_reconcile",
                "deterministic_replay": True,
                "derived": {"owner_rows": 10, "global_stage_rows": 20},
                "source_calls": 0,
                "graph_writes": 0,
                "stage_domain_writes": 0,
            }
        ),
        encoding="utf-8",
    )

    result = load_qualification(evidence)

    assert result.replay_digest.startswith("sha256:")
    assert result.stage_domain_writes == 0
