from __future__ import annotations

import pytest
from src.crm_deal_identity_repair.execution_models import (
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
)


def _manifest(
    *,
    environment: str = "staging",
    unit_ceiling: int = 1,
    stop_conditions: tuple[str, ...] = ("boundary_drift", "partial_mutation"),
    execution_allowed: bool = False,
) -> RepairExecutionBoundaryManifest:
    return RepairExecutionBoundaryManifest(
        repair_id="repair-300",
        artifact_id="a" * 32,
        artifact_manifest_hmac="b" * 64,
        inventory_digest="sha256:" + "c" * 64,
        repository_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        configuration_digest="sha256:" + "f" * 64,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment=environment,
        approval_reference="opaque-ticket-42",
        unit_ceiling=unit_ceiling,
        stop_conditions=stop_conditions,
        source_instance_id="legacy-default",
        control_instance_id="legacy-default",
        rollback_authority_reference="rollback-ticket",
        rollback_authority_policy="reviewed-only",
        graph_boundary_digest="sha256:" + "1" * 64,
        inventory_row_count=1,
        eligible_unit_count=1,
        negative_control_count=0,
        execution_allowed=execution_allowed,
    )


def test_manifest_is_staging_only_and_canonicalizes_stop_conditions() -> None:
    manifest = _manifest(stop_conditions=("partial_mutation", "boundary_drift"))
    assert manifest.stop_conditions == ("boundary_drift", "partial_mutation")
    with pytest.raises(ValueError, match="staging-only"):
        _manifest(environment="production")


def test_manifest_requires_positive_ceiling_and_stable_digest() -> None:
    assert (
        _manifest().manifest_digest
        == _manifest(stop_conditions=("partial_mutation", "boundary_drift")).manifest_digest
    )
    with pytest.raises(ValueError, match="positive"):
        _manifest(unit_ceiling=0)
    with pytest.raises(ValueError, match="non-executable"):
        _manifest(execution_allowed=True)


def test_manifest_rejects_duplicate_or_unknown_stop_conditions() -> None:
    with pytest.raises(ValueError, match="unique"):
        _manifest(stop_conditions=("boundary_drift", "boundary_drift"))
    with pytest.raises(ValueError, match="unknown"):
        _manifest(stop_conditions=("not_a_stop_condition",))


def test_boundary_snapshot_rejects_noncanonical_source_record_keys() -> None:
    with pytest.raises(ValueError, match="sorted"):
        RepairBoundarySnapshot(
            "legacy-default",
            "legacy-default",
            ("b", "a"),
            "sha256:" + "0" * 64,
            2,
            1,
            1,
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
            "sha256:" + "4" * 64,
        )
