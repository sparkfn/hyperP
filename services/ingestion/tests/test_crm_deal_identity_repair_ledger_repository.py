from __future__ import annotations

import uuid
from dataclasses import replace
from typing import cast

import pytest
from neo4j import Record
from neo4j.spatial import WGS84Point
from neo4j.time import Date, Duration
from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
    RepairRunStatus,
)
from src.graph.crm_deal_identity_repair_boundary_evidence import neo4j_json_value
from src.graph.crm_deal_identity_repair_ledger import (
    ExpectedRepairBoundaryDriftError,
    _assert_requested_boundary,
    _canonical_boundary_evidence,
    _control_digest_value,
    _stored_qualification_from_record,
)
from src.graph.queries import crm_deal_identity_repair_ledger as queries
from src.models import JsonValue


def _manifest() -> RepairExecutionBoundaryManifest:
    return RepairExecutionBoundaryManifest(
        repair_id="repair-300",
        artifact_id="a" * 32,
        artifact_manifest_hmac="b" * 64,
        inventory_digest="sha256:" + "c" * 64,
        repository_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        configuration_digest="sha256:" + "f" * 64,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference="approval-300",
        unit_ceiling=1,
        stop_conditions=("boundary_drift", "partial_mutation"),
        source_instance_id="portal-a",
        control_instance_id="control-a",
        rollback_authority_reference="rollback-300",
        rollback_authority_policy="reviewed-only",
        graph_boundary_digest="sha256:" + "1" * 64,
        inventory_row_count=2,
        eligible_unit_count=1,
        negative_control_count=1,
    )


def _record() -> dict[str, object]:
    manifest = _manifest()
    pks = ("pk-1", "pk-2")
    manifest_json = canonical_json_bytes(manifest.to_dict()).decode("utf-8")
    pks_json = canonical_json_bytes({"source_record_pks": list(pks)}).decode("utf-8")
    expected = {
        "manifest_digest": manifest.manifest_digest,
        "artifact_id": manifest.artifact_id,
        "artifact_manifest_hmac": manifest.artifact_manifest_hmac,
        "inventory_digest": manifest.inventory_digest,
        "boundary_digest": manifest.graph_boundary_digest,
        "source_instance_id": manifest.source_instance_id,
        "control_instance_id": manifest.control_instance_id,
        "source_record_pks_json": pks_json,
        "manifest_json": manifest_json,
        "inventory_row_count": 2,
        "eligible_unit_count": 1,
        "negative_control_count": 1,
        "execution_allowed": False,
    }
    return {
        "run_id": str(uuid.uuid5(uuid.NAMESPACE_URL, manifest.qualification_identity)),
        "qualification_identity": manifest.qualification_identity,
        "status": "qualified",
        "qualification_link_count": 1,
        "boundaries": [expected],
        **expected,
    }


def test_stored_qualification_reads_full_canonical_boundary_identity() -> None:
    stored = _stored_qualification_from_record("repair-300", cast(Record, _record()))
    assert stored.manifest == _manifest()
    assert stored.source_record_pks == ("pk-1", "pk-2")
    assert (
        stored.run.inventory_row_count,
        stored.run.eligible_unit_count,
        stored.run.negative_control_count,
    ) == (2, 1, 1)


@pytest.mark.parametrize(
    "key", ("manifest_digest", "qualification_identity", "inventory_row_count")
)
def test_stored_qualification_rejects_run_identity_or_count_corruption(key: str) -> None:
    record = _record()
    record[key] = "sha256:" + "9" * 64 if key != "inventory_row_count" else 9
    with pytest.raises(RuntimeError):
        _stored_qualification_from_record("repair-300", cast(Record, record))


def test_stored_qualification_rejects_noncanonical_or_mismatched_boundary() -> None:
    record = _record()
    record["manifest_json"] = '{"repair_id":"repair-300"}'
    with pytest.raises(RuntimeError):
        _stored_qualification_from_record("repair-300", cast(Record, record))
    record = _record()
    cast(list[object], record["boundaries"])[0] = {"manifest_digest": "bad"}
    with pytest.raises(RuntimeError):
        _stored_qualification_from_record("repair-300", cast(Record, record))


@pytest.mark.parametrize(
    "reason",
    (
        "missing_source_record",
        "source_instance_mismatch",
        "source_instance_disabled",
        "missing_binding",
        "missing_control_evidence",
        "binding_mismatch",
        "persisted_boundary_change",
    ),
)
def test_expected_drift_reasons_produce_read_only_status(
    reason: RepairBoundaryDriftReason,
) -> None:
    manifest = _manifest()
    run = RepairQualificationRun(
        "repair-300",
        str(uuid.uuid5(uuid.NAMESPACE_URL, manifest.qualification_identity)),
        manifest.qualification_identity,
        manifest,
        manifest.graph_boundary_digest,
        "qualified",
    )
    status = RepairRunStatus.drifted(run, reason)
    assert (status.admissibility, status.reason_code, status.execution_allowed) == (
        "drifted",
        reason,
        False,
    )
    assert status.manifest == manifest


def test_boundary_queries_are_read_only() -> None:
    for query in (
        queries.READ_SOURCE_RECORD_BOUNDARY,
        queries.READ_INSTANCE_CONTROL_BOUNDARY,
        queries.READ_CONTROL_DISPATCH_EVIDENCE,
        queries.READ_CONTROL_NODES,
        queries.READ_CONTROL_RELATIONSHIPS,
        queries.READ_STALE_RUN_CONTROL_EVIDENCE,
        queries.READ_STALE_RUN_ASSOCIATIONS,
    ):
        upper = query.upper()
        assert "ELEMENTID(" not in upper
        for forbidden in ("CREATE", "MERGE", " SET ", "DELETE", "REMOVE"):
            assert forbidden not in upper


def test_control_relationship_query_is_limited_to_272_topology() -> None:
    upper = queries.READ_CONTROL_RELATIONSHIPS.upper()
    for relationship_type in (
        "HAS_ATTEMPT",
        "ACTIVE_ATTEMPT",
        "CHECKPOINT_FOR",
        "PRODUCED_BY",
        "HAS_LOGICAL_RUN",
        "HAS_STREAM",
        "HAS_KNOWN_OWNER_SET",
        "HAS_COVERAGE",
        "HAS_OWNER_RETRY",
        "HAS_MEMBER",
        "HAS_SUCCESSOR",
        "HAS_STAGE_HISTORY_UNIT",
        "HAS_STAGE_HISTORY_REVIEW_COMMAND",
        "CONTAINS_STAGE_HISTORY_OCCURRENCE",
        "HAS_STAGE_HISTORY_ACCOUNTING",
        "HAS_STAGE_HISTORY_RETRY",
    ):
        assert relationship_type in upper
    assert "PART_OF_RUN" not in upper
    assert "SOURCERECORD" not in upper


def test_unordered_control_evidence_has_a_deterministic_boundary_digest() -> None:
    first: dict[str, JsonValue] = {
        "binding_count": 1,
        "binding_ownership_count": 1,
        "requested_binding_count": 1,
        "requested_ownership_count": 1,
        "binding_source_instance_ids": ["source-a", "source-b"],
        "binding_owner_instance_ids": ["source-b", "source-a"],
        "owned_binding_source_instance_ids": ["source-a"],
        "dispatch_count": 1,
        "dispatch_evidence": [
            {"blocked": False, "blocked_generation_id": "two"},
            {"blocked": False, "blocked_generation_id": "one"},
        ],
        "control_nodes": [
            {"labels": ["IngestRun", "Control"], "ingest_run_id": "two"},
            {"labels": ["Control", "IngestRun"], "ingest_run_id": "one"},
        ],
        "control_relationships": [
            {"left_labels": ["IngestRun", "Control"], "relationship_type": "FROM_SOURCE"},
            {"left_labels": ["Control", "IngestRun"], "relationship_type": "HAS_ATTEMPT"},
        ],
    }
    second: dict[str, JsonValue] = {
        **first,
        "binding_source_instance_ids": ["source-b", "source-a"],
        "binding_owner_instance_ids": ["source-a", "source-b"],
        "dispatch_evidence": list(reversed(first["dispatch_evidence"])),
        "control_nodes": list(reversed(first["control_nodes"])),
        "control_relationships": list(reversed(first["control_relationships"])),
    }
    normalized_first = _canonical_boundary_evidence(first)
    normalized_second = _canonical_boundary_evidence(second)
    assert isinstance(normalized_first, dict)
    assert isinstance(normalized_second, dict)
    first_value = _control_digest_value(normalized_first)
    second_value = _control_digest_value(normalized_second)
    assert first_value == second_value
    assert object_digest(b"test-control-boundary\x00", first_value) == object_digest(
        b"test-control-boundary\x00", second_value
    )


def test_boundary_evidence_conversion_is_canonical_and_fails_closed() -> None:
    assert neo4j_json_value(
        {"date": Date(2026, 8, 28), "duration": Duration(months=1, seconds=3)}
    ) == {"date": "2026-08-28", "duration": "P1MT3S"}
    assert neo4j_json_value(WGS84Point((1.0, 2.0))) == {
        "coordinates": [1.0, 2.0],
        "srid": 4326,
    }
    with pytest.raises(RuntimeError, match="non-finite"):
        neo4j_json_value(float("nan"))
    with pytest.raises(RuntimeError, match="unsupported"):
        neo4j_json_value(object())


def test_canonical_boundary_evidence_orders_rows_without_losing_multiplicity() -> None:
    evidence: JsonValue = [
        {"labels": ["StageHistoryUnit"], "properties": {"unit_id": "unit-b"}},
        {"labels": ["StageHistoryUnit"], "properties": {"unit_id": "unit-a"}},
        {"labels": ["StageHistoryUnit"], "properties": {"unit_id": "unit-a"}},
    ]
    normalized = _canonical_boundary_evidence(evidence)
    assert normalized == [
        {"labels": ["StageHistoryUnit"], "properties": {"unit_id": "unit-a"}},
        {"labels": ["StageHistoryUnit"], "properties": {"unit_id": "unit-a"}},
        {"labels": ["StageHistoryUnit"], "properties": {"unit_id": "unit-b"}},
    ]


def test_property_list_order_is_boundary_evidence() -> None:
    first: JsonValue = [
        {
            "labels": ["StageHistoryUnit", "Control"],
            "properties": {"ordered_checkpoints": ["first", "second"]},
        },
        {"labels": ["BitrixBackfillGeneration"], "properties": {"generation_id": "a"}},
    ]
    second: JsonValue = [
        {"labels": ["BitrixBackfillGeneration"], "properties": {"generation_id": "a"}},
        {
            "labels": ["Control", "StageHistoryUnit"],
            "properties": {"ordered_checkpoints": ["second", "first"]},
        },
    ]
    normalized_first = _canonical_boundary_evidence(first)
    normalized_second = _canonical_boundary_evidence(second)
    assert normalized_first != normalized_second
    assert object_digest(b"test-property-order\x00", {"rows": normalized_first}) != object_digest(
        b"test-property-order\x00", {"rows": normalized_second}
    )


def test_qualification_query_writes_only_repair_control_labels() -> None:
    query = queries.QUALIFY_REPAIR_RUN
    assert "RepairExecutionBoundary" in query
    assert "CrmDealRepairRun" in query
    assert "SourceRecord" not in query
    assert "Person" not in query


def test_run_schema_keeps_deterministic_run_and_repair_ids_separately_unique() -> None:
    schema = queries.CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA
    assert any(
        "crm_deal_repair_run_id_unique" in statement and "n.run_id" in statement
        for statement in schema
    )
    assert any(
        "crm_deal_repair_run_repair_id_unique" in statement and "n.repair_id" in statement
        for statement in schema
    )


def test_admission_boundary_rejects_current_inventory_digest_or_count_drift() -> None:
    current = RepairBoundarySnapshot(
        "portal-a",
        "control-a",
        ("pk-1", "pk-2"),
        "sha256:" + "c" * 64,
        2,
        1,
        1,
        "sha256:" + "1" * 64,
        "sha256:" + "2" * 64,
        "sha256:" + "3" * 64,
        "sha256:" + "4" * 64,
    )
    manifest = replace(
        _manifest(),
        inventory_digest=current.inventory_digest,
        graph_boundary_digest=current.boundary_digest,
    )
    _assert_requested_boundary(manifest, current)
    changed = replace(current, inventory_digest="sha256:" + "d" * 64)
    with pytest.raises(ExpectedRepairBoundaryDriftError) as drift:
        _assert_requested_boundary(manifest, changed)
    assert drift.value.reason == "persisted_boundary_change"
