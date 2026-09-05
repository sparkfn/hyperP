"""Cutover state transitions preserve frozen/rejected generation safety."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pytest import MonkeyPatch
from src.bitrix_backfill_control import CONTROL_COMMANDS, BitrixBackfillControl, load_qualification
from src.graph.queries.bitrix_backfill import (
    ACTIVATE_BITRIX_SUCCESSOR_GENERATION,
    ALLOCATE_BITRIX_BACKFILL_GENERATION,
    ALLOCATE_BITRIX_SUCCESSOR_GENERATION,
    ATTACH_BACKFILL_LOGICAL_RUN,
    CAS_BITRIX_BACKFILL_GENERATION_STATUS,
    CONFIRM_BITRIX_SUCCESSOR_PUBLICATION,
    FREEZE_BITRIX_BACKFILL_GENERATION,
    GET_BITRIX_GENERATION_CATEGORY_MAPPING,
    GET_BITRIX_SUCCESSOR_PUBLICATION_OCCURRENCE,
    GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION,
    RECORD_BITRIX_QUALIFICATION,
    REJECT_BITRIX_BACKFILL_GENERATION,
    SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR,
    VERIFY_BITRIX_SUCCESSOR_TAIL,
)
from src.graph.queries.ingestion_control import CREATE_LOGICAL_RUN_AND_ATTEMPT
from src.graph.queries.source_records import (
    CREATE_INGEST_RUN,
    CREATE_OR_REUSE_WORKER_INGEST_RUN,
)
from src.ingestion_config import BitrixOpenLinesConfig


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
        "recover-successor",
        "verify-tail",
        "rollback-status",
    }


def test_zero_write_successor_recovery_is_fail_closed_and_fences_the_old_stream() -> None:
    assert "corrective.status = 'accepted'" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "successor.generation_kind = 'live_successor'" in (SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR)
    assert "logical.status = 'failed'" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "stream.status = 'superseded'" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "stream.fence_lock_version" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "successor.status = 'superseded'" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "successor.superseded_by_generation_id" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "successor.supersession_evidence_digest" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "dispatch.blocked = true" in SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR


def test_recovery_only_locks_the_failed_generations_exact_stream_fence() -> None:
    query = SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    assert "collect({relation: generation_stream, stream: stream})" in query
    for field in (
        "logical_run_id",
        "ingest_run_id",
        "attempt_generation",
        "stream_generation",
        "fencing_token",
    ):
        assert f"binding.relation.{field} = binding.stream.{field}" in query
    assert "UNWIND CASE WHEN retry THEN [] ELSE bindings END AS binding" in query
    assert "binding.relation AS generation_stream, binding.stream AS stream" in query
    assert "generation_streams[index]" not in query
    assert "streams[index]" not in query


def test_zero_write_recheck_occurs_after_stream_fence_locking() -> None:
    query = SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR
    lock_position = query.index("stream.fence_lock_version =")
    coverage_position = query.index("OPTIONAL MATCH (successor)-[:HAS_COVERAGE]")
    zero_write_position = query.index("material_write_count = 0")
    assert lock_position < coverage_position < zero_write_position


def test_successor_allocation_serializes_competing_replacements() -> None:
    query = ALLOCATE_BITRIX_SUCCESSOR_GENERATION
    lock_position = query.index("SET corrective.successor_lock_version")
    competing_position = query.index("OPTIONAL MATCH (corrective)-[:HAS_SUCCESSOR]")
    allocation_position = query.index("MERGE (successor:BitrixBackfillGeneration")
    assert lock_position < competing_position < allocation_position


def test_recovery_requires_distinct_successor_identity() -> None:
    control = object.__new__(BitrixBackfillControl)

    with pytest.raises(ValueError, match="distinct"):
        control.recover_successor(
            corrective_generation_id="corrective",
            failed_successor_generation_id="successor",
            replacement_successor_generation_id="successor",
            manifest=object(),
            successor_boundary_digest="sha256:boundary",
            occurrence="2026-08-11",
            actor="operator",
            reason="zero-write transaction memory failure",
        )


def test_recovery_supersedes_before_activating_replacement(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.side_effect = [
        SimpleNamespace(status="accepted", configuration_digest="sha256:runtime"),
        SimpleNamespace(
            generation_kind="live_successor",
            corrective_generation_id="corrective",
            material_write_count=0,
        ),
    ]
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    monkeypatch.setattr(
        "src.bitrix_backfill_control._require_runtime_configuration",
        lambda _expected, **_kwargs: None,
    )
    control.activate = Mock(return_value="replacement-canvas")
    manifest = Mock()

    canvas_id = control.recover_successor(
        corrective_generation_id="corrective",
        failed_successor_generation_id="successor-1",
        replacement_successor_generation_id="successor-2",
        manifest=manifest,
        successor_boundary_digest="sha256:replacement-boundary",
        occurrence="2026-08-11",
        actor="operator",
        reason="zero-write transaction memory failure",
    )

    assert canvas_id == "replacement-canvas"
    repository.supersede_zero_write_successor.assert_called_once()
    control.activate.assert_called_once_with(
        corrective_generation_id="corrective",
        successor_generation_id="successor-2",
        manifest=manifest,
        successor_boundary_digest="sha256:replacement-boundary",
        occurrence="2026-08-11",
        actor="operator",
    )


def test_allocation_rejects_an_unbound_configuration_digest_before_mutation(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    runtime_config = BitrixOpenLinesConfig()
    monkeypatch.setattr(
        "src.bitrix_backfill_control.get_ingestion_config",
        lambda: SimpleNamespace(bitrix_openlines=runtime_config),
    )
    manifest = SimpleNamespace(
        minimum_fence_image_digest="sha256:image",
        validate_new_operational_manifest=lambda: None,
    )

    with pytest.raises(ValueError, match="must include CRM categories"):
        control.allocate(
            "corrective",
            manifest,
            repository_sha="repository-sha",
            image_digest="sha256:image",
            configuration_digest=(
                "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
            ),
            source_contract_uuid="source-contract",
            boundary_digest="sha256:boundary",
        )

    repository.allocate_generation.assert_not_called()


def test_acceptance_revalidates_legacy_digest_against_generation_mapping(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="qualified",
        configuration_digest=(
            "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
        ),
    )
    repository.get_generation_category_mapping.return_value = {
        "2": "eko",
        "7": "fundbox",
        "8": "speedzone",
    }
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    monkeypatch.setattr(
        "src.bitrix_backfill_control.get_ingestion_config",
        lambda: SimpleNamespace(
            bitrix_openlines=BitrixOpenLinesConfig(
                included_crm_category_ids=["2", "7", "8"],
                entity_by_crm_category_id={
                    "2": "eko",
                    "7": "fundbox",
                    "8": "speedzone",
                },
            )
        ),
    )

    control.accept("corrective", actor="operator", reason="reviewed evidence")

    repository.get_generation_category_mapping.assert_called_once_with("corrective")
    repository.transition.assert_called_once()


def test_activation_rejects_a_non_container_runtime_configuration(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="accepted",
        configuration_digest="sha256:frozen-runtime",
    )
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    runtime_config = SimpleNamespace(
        bitrix_openlines=BitrixOpenLinesConfig(
            included_crm_category_ids=["2"],
            entity_by_crm_category_id={"2": "eko"},
        )
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_control.get_ingestion_config",
        lambda: runtime_config,
    )
    monkeypatch.setattr(
        "src.bitrix_backfill_control.bitrix_configuration_digest",
        lambda *_args: "sha256:wrong-host-config",
    )

    with pytest.raises(RuntimeError, match="deployed container ingestion config"):
        control.activate(
            corrective_generation_id="corrective",
            successor_generation_id="successor-2",
            manifest=Mock(),
            successor_boundary_digest="sha256:replacement-boundary",
            occurrence="2026-08-11",
            actor="operator",
        )

    repository.allocate_successor.assert_not_called()


def test_legacy_explicit_category_digest_requires_accepted_mapping_evidence(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="accepted",
        configuration_digest=(
            "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
        ),
    )
    repository.get_generation_category_mapping.return_value = {
        "2": "eko",
        "7": "fundbox",
        "8": "speedzone",
    }
    repository.allocate_successor.side_effect = RuntimeError("runtime gate passed")
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    monkeypatch.setattr(
        "src.bitrix_backfill_control.get_ingestion_config",
        lambda: SimpleNamespace(
            bitrix_openlines=BitrixOpenLinesConfig(
                included_crm_category_ids=["2", "7", "8"],
                entity_by_crm_category_id={
                    "2": "eko",
                    "7": "fundbox",
                    "8": "speedzone",
                },
            )
        ),
    )

    with pytest.raises(RuntimeError, match="runtime gate passed"):
        control.activate(
            corrective_generation_id="corrective",
            successor_generation_id="successor-2",
            manifest=Mock(),
            successor_boundary_digest="sha256:replacement-boundary",
            occurrence="2026-08-11",
            actor="operator",
        )

    repository.get_generation_category_mapping.assert_called_once_with("corrective")
    repository.allocate_successor.assert_called_once()
    assert "generation.status IN ['qualified', 'accepted']" in (
        GET_BITRIX_GENERATION_CATEGORY_MAPPING
    )
    assert "coverage.category_id" in GET_BITRIX_GENERATION_CATEGORY_MAPPING
    assert "coverage.entity_key" in GET_BITRIX_GENERATION_CATEGORY_MAPPING


def test_legacy_explicit_category_digest_rejects_changed_mapping_evidence(
    monkeypatch: MonkeyPatch,
) -> None:
    repository = Mock()
    repository.get_generation.return_value = SimpleNamespace(
        status="accepted",
        configuration_digest=(
            "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
        ),
    )
    repository.get_generation_category_mapping.return_value = {
        "2": "eko",
        "7": "fundbox",
        "8": "different-entity",
    }
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    monkeypatch.setattr(
        "src.bitrix_backfill_control.get_ingestion_config",
        lambda: SimpleNamespace(
            bitrix_openlines=BitrixOpenLinesConfig(
                included_crm_category_ids=["2", "7", "8"],
                entity_by_crm_category_id={
                    "2": "eko",
                    "7": "fundbox",
                    "8": "speedzone",
                },
            )
        ),
    )

    with pytest.raises(RuntimeError, match="generation category mapping evidence"):
        control.recover_successor(
            corrective_generation_id="corrective",
            failed_successor_generation_id="successor-1",
            replacement_successor_generation_id="successor-2",
            manifest=Mock(),
            successor_boundary_digest="sha256:replacement-boundary",
            occurrence="2026-08-11",
            actor="operator",
            reason="zero-write failure",
        )

    repository.supersede_zero_write_successor.assert_not_called()
    repository.allocate_successor.assert_not_called()


def test_active_successor_retry_returns_the_confirmed_canvas_without_redispatch(
    monkeypatch: MonkeyPatch,
) -> None:
    manifest = SimpleNamespace(
        digest="sha256:inventory",
        validate_new_operational_manifest=lambda: None,
    )
    repository = Mock()
    repository.get_generation.side_effect = [
        SimpleNamespace(status="accepted", configuration_digest="sha256:runtime"),
        SimpleNamespace(status="active"),
    ]
    repository.get_confirmed_successor_canvas.return_value = "canvas-1"
    control = object.__new__(BitrixBackfillControl)
    control._repository = repository
    control._manifest_for = Mock(return_value=manifest)
    monkeypatch.setattr(
        "src.bitrix_backfill_control._require_runtime_configuration",
        lambda _expected, **_kwargs: None,
    )

    canvas_id = control.activate(
        corrective_generation_id="corrective",
        successor_generation_id="successor-2",
        manifest=manifest,
        successor_boundary_digest="sha256:replacement-boundary",
        occurrence="2026-08-11",
        actor="operator",
    )

    assert canvas_id == "canvas-1"
    repository.activate_successor.assert_not_called()
    repository.confirm_successor_publication.assert_not_called()
    repository.get_confirmed_successor_canvas.assert_called_once()
    assert "successor.activation_evidence_digest = $evidence_digest" in (
        GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION
    )
    assert "status: 'published'" in GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION
    assert "occurrence: $occurrence" in GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION
    assert "outbox.canvas_id IS NOT NULL" in GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION


def test_acceptance_and_activation_use_distinct_cas_bound_generations() -> None:
    assert "generation.status IN $expected_statuses" in CAS_BITRIX_BACKFILL_GENERATION_STATUS
    assert "generation.repository_sha = $repository_sha" in (CAS_BITRIX_BACKFILL_GENERATION_STATUS)
    assert "generation.status = 'frozen'" in RECORD_BITRIX_QUALIFICATION
    assert "successor.status = 'allocated'" in ALLOCATE_BITRIX_SUCCESSOR_GENERATION
    assert "corrective.status = 'accepted'" in ACTIVATE_BITRIX_SUCCESSOR_GENERATION
    assert "successor.status = 'activating'" in ACTIVATE_BITRIX_SUCCESSOR_GENERATION
    assert "successor.status = 'active'" in CONFIRM_BITRIX_SUCCESSOR_PUBLICATION
    assert "outbox.status = 'published'" in CONFIRM_BITRIX_SUCCESSOR_PUBLICATION


def test_predecessor_freeze_is_generation_scoped() -> None:
    assert "MERGE (generation)-[generation_stream:HAS_STREAM]->(stream)" in (
        ATTACH_BACKFILL_LOGICAL_RUN
    )
    assert "generation_stream.fencing_token = stream.fencing_token" in (ATTACH_BACKFILL_LOGICAL_RUN)
    assert "collect(generation_stream) AS generation_streams" in (FREEZE_BITRIX_BACKFILL_GENERATION)
    for field in (
        "logical_run_id",
        "ingest_run_id",
        "attempt_generation",
        "stream_generation",
        "fencing_token",
    ):
        snapshot = f"generation_stream.{field}, stream.{field}"
        assert snapshot in FREEZE_BITRIX_BACKFILL_GENERATION
        assert snapshot in REJECT_BITRIX_BACKFILL_GENERATION
    assert "generation_stream.status = 'superseded'" in (FREEZE_BITRIX_BACKFILL_GENERATION)
    assert (
        ")\nWITH generation, logicals\nUNWIND logicals AS logical"
        in FREEZE_BITRIX_BACKFILL_GENERATION
    )
    assert "collect(old_relation) AS old_relations" in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert "relation.status = 'superseded'" in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert (
        "count(CASE WHEN coverage.stream_key IN expected_streams THEN 1 END)"
        in VERIFY_BITRIX_SUCCESSOR_TAIL
    )
    assert "AS acceptable_coverage_count" in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert "coverage.stream_key IN expected_streams" in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert (
        "collect(DISTINCT CASE WHEN coverage.stream_key IN expected_streams "
        "THEN coverage.stream_key END)"
    ) in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert "all(expected IN expected_streams WHERE expected IN coverage_streams)" in (
        VERIFY_BITRIX_SUCCESSOR_TAIL
    )
    assert "NOT coverage.disposition IN ['conflict', 'failed']" in (VERIFY_BITRIX_SUCCESSOR_TAIL)
    assert "coverage.disposition NOT IN" not in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert "collect(coverage)" not in VERIFY_BITRIX_SUCCESSOR_TAIL
    assert "old_streams" not in VERIFY_BITRIX_SUCCESSOR_TAIL


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


def test_successor_resume_occurrence_comes_from_published_outbox() -> None:
    assert "generation_kind: 'live_successor'" in GET_BITRIX_SUCCESSOR_PUBLICATION_OCCURRENCE
    assert "status: 'published'" in GET_BITRIX_SUCCESSOR_PUBLICATION_OCCURRENCE
    assert "outbox.occurrence IS NOT NULL" in GET_BITRIX_SUCCESSOR_PUBLICATION_OCCURRENCE
