"""Focused detached approval-overlay contract tests for #310 allocation."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.approval_overlay import (
    allocate_units,
    verify_approval_overlay,
    verify_sealed_approval_overlay,
)
from src.crm_deal_identity_repair.execution_models import RepairExecutionBoundaryManifest
from src.models import JsonValue

_SECRET = b"repair-310-overlay-secret"
_DIGEST = "sha256:" + "a" * 64


def _manifest() -> RepairExecutionBoundaryManifest:
    return RepairExecutionBoundaryManifest(
        repair_id="repair-310",
        artifact_id="a" * 32,
        artifact_manifest_hmac="b" * 64,
        inventory_digest="sha256:" + "c" * 64,
        repository_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        configuration_digest="sha256:" + "f" * 64,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference="approval-310",
        unit_ceiling=2,
        stop_conditions=("boundary_drift",),
        source_instance_id="source-310",
        control_instance_id="control-310",
        rollback_authority_reference="rollback-310",
        rollback_authority_policy="reviewed-only",
        graph_boundary_digest=_DIGEST,
        inventory_row_count=2,
        eligible_unit_count=2,
        negative_control_count=0,
    )


def _payload() -> dict[str, JsonValue]:
    manifest = _manifest()
    return {
        "approval_reference": manifest.approval_reference,
        "artifact_id": manifest.artifact_id,
        "artifact_manifest_hmac": manifest.artifact_manifest_hmac,
        "inventory_digest": manifest.inventory_digest,
        "manifest_digest": manifest.manifest_digest,
        "repository_sha": manifest.repository_sha,
        "image_digest": manifest.image_digest,
        "configuration_digest": manifest.configuration_digest,
        "source_contract_uuid": manifest.source_contract_uuid,
        "unit_ceiling": manifest.unit_ceiling,
        "rows": [
            {
                "inventory_key": "inventory-1",
                "source_record_pk": "source-1",
                "inventory_fingerprint": "sha256:" + "1" * 64,
                "disposition": "executable",
            },
            {
                "inventory_key": "inventory-2",
                "source_record_pk": "source-2",
                "inventory_fingerprint": "sha256:" + "2" * 64,
                "disposition": "blocked",
            },
        ],
    }


def _sealed(payload: dict[str, JsonValue]) -> bytes:
    signature = hmac.new(
        _SECRET,
        b"crm-deal-identity-repair-approval-overlay-v1\x00" + canonical_json_bytes(payload),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps({"payload": payload, "hmac": "sha256:" + signature}).encode("utf-8")


def test_sealed_overlay_binds_every_qualified_row_and_allocates_only_executable_rows() -> None:
    manifest = _manifest()
    overlay = verify_sealed_approval_overlay(
        _sealed(_payload()),
        manifest=manifest,
        signing_secret=_SECRET,
    )
    units = allocate_units(overlay, run_id="run-310", manifest=manifest, generation=1)

    assert tuple(row.source_record_pk for row in overlay.rows) == ("source-1", "source-2")
    assert len(units) == 1
    assert units[0].inventory_fingerprint == "sha256:" + "1" * 64


def test_overlay_rejects_tampering_changed_identity_and_incomplete_coverage() -> None:
    manifest = _manifest()
    with pytest.raises(ValueError, match="HMAC"):
        verify_sealed_approval_overlay(
            _sealed(_payload()).replace(b"inventory-1", b"inventory-x"),
            manifest=manifest,
            signing_secret=_SECRET,
        )

    incomplete = _payload()
    rows = incomplete["rows"]
    assert isinstance(rows, list)
    incomplete["rows"] = rows[:1]
    with pytest.raises(ValueError, match="cover"):
        verify_approval_overlay(incomplete, manifest=manifest)

    duplicate = _payload()
    duplicate_rows = duplicate["rows"]
    assert isinstance(duplicate_rows, list)
    duplicate["rows"] = [duplicate_rows[0], duplicate_rows[0]]
    with pytest.raises(ValueError, match="unique"):
        verify_approval_overlay(duplicate, manifest=manifest)


def test_overlay_rejects_noncanonical_inventory_key_even_with_valid_shape() -> None:
    payload = _payload()
    rows = payload["rows"]
    assert isinstance(rows, list)
    first = rows[0]
    assert isinstance(first, dict)
    first["inventory_key"] = "not-qualified-source-1"
    with pytest.raises(ValueError, match="inventory keys"):
        verify_approval_overlay(payload, manifest=_manifest())
