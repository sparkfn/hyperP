"""CLI coverage for the default-off #310 repair control plane."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest
from src.crm_deal_identity_repair.allocation import AllocationPlan
from src.crm_deal_identity_repair.approval_overlay import ApprovalOverlay, ApprovalRow
from src.crm_deal_identity_repair.control_models import (
    RepairControlStatus,
    RepairDispatchLease,
    control_token_digest,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairExecutionBoundaryManifest,
    RepairQualificationRun,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair_control import main

_DIGEST = f"sha256:{'a' * 64}"
_ARTIFACT_HMAC = "b" * 64
_ARTIFACT_ID = "c" * 32
_REPOSITORY_SHA = "d" * 40
_INGESTION_ROOT = Path(__file__).resolve().parents[1]
_CONTROL_MODULE = "src.crm_deal_identity_repair_control"


def _run_control_module(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    python_path = environment.get("PYTHONPATH")
    for key in tuple(environment):
        if key.upper().startswith(("CRM_DEAL_IDENTITY_REPAIR_", "NEO4J_")):
            environment.pop(key)
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(_INGESTION_ROOT), python_path) if part
    )
    environment["DEPLOYMENT_ENVIRONMENT"] = "development"
    # This unsupported URI must fail locally before any network access if the gate regresses.
    environment["NEO4J_URI"] = "unsupported://127.0.0.1:1"
    environment["NEO4J_USER"] = "test-user"
    environment["NEO4J_PASSWORD"] = "test-password"
    return subprocess.run(
        [sys.executable, "-m", _CONTROL_MODULE, *arguments],
        cwd=_INGESTION_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_module_execution_displays_help() -> None:
    result = _run_control_module("--help")

    assert result.returncode == 0
    assert result.stdout
    assert "usage:" in result.stdout.lower()
    assert "python -m src.crm_deal_identity_repair_control" in result.stdout


def test_module_execution_dispatches_to_status_handler() -> None:
    result = _run_control_module("status", "--repair-id", "subprocess-dispatch-probe")

    assert result.returncode == 1
    assert "in _status" in result.stderr
    assert "CRM-deal repair inventory requires DEPLOYMENT_ENVIRONMENT=staging" in result.stderr


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class _Client:
    def __init__(self, _settings: object) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _item() -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="deal-1",
        source_record_pk="pk-1",
        deal_id="1",
        partition="ownership_repair",
        graph_fingerprint=_DIGEST,
        stored_payload_fingerprint=_DIGEST,
        payload={},
    )


def _run() -> RepairQualificationRun:
    manifest = RepairExecutionBoundaryManifest(
        repair_id="repair-1",
        artifact_id=_ARTIFACT_ID,
        artifact_manifest_hmac=_ARTIFACT_HMAC,
        inventory_digest=_DIGEST,
        repository_sha=_REPOSITORY_SHA,
        image_digest=_DIGEST,
        configuration_digest=_DIGEST,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference="approval-reference",
        unit_ceiling=1,
        stop_conditions=("boundary_drift",),
        source_instance_id="legacy-default",
        control_instance_id="legacy-default",
        rollback_authority_reference="rollback-reference",
        rollback_authority_policy="manual",
        graph_boundary_digest=_DIGEST,
        inventory_row_count=1,
        eligible_unit_count=1,
        negative_control_count=0,
    )
    return RepairQualificationRun(
        "repair-1",
        "run-1",
        manifest.qualification_identity,
        manifest,
        _DIGEST,
        "qualified",
    )


def _overlay(run: RepairQualificationRun) -> ApprovalOverlay:
    item = _item()
    manifest = run.manifest
    return ApprovalOverlay(
        "approval-1",
        run.repair_id,
        run.run_id,
        run.qualification_identity,
        run.artifact_id,
        run.artifact_manifest_hmac,
        run.inventory_digest,
        run.inventory_row_count,
        run.boundary_digest,
        manifest.repository_sha,
        manifest.image_digest,
        manifest.configuration_digest,
        manifest.source_contract_uuid,
        manifest.approval_reference,
        manifest.unit_ceiling,
        (
            ApprovalRow(
                item.inventory_key,
                item.source_record_pk,
                item.graph_fingerprint,
                item.stored_payload_fingerprint,
                "executable",
            ),
        ),
        "approval-key-1",
        _DIGEST,
    )


def _status_arguments() -> tuple[str, ...]:
    return ("status", "--repair-id", "repair-1")


def _control_arguments() -> tuple[str, ...]:
    return (
        "allocate",
        "--repair-id",
        "repair-1",
        "--run-id",
        "run-1",
        "--owner-id",
        "owner-1",
        "--expected-revision",
        "1",
        "--approval-id",
        "approval-1",
    )


def test_status_is_read_only_when_repair_is_disabled_and_reports_separate_control_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Status may read disabled staging state but never enters a write control path."""

    import src.config as config_module
    import src.graph.client as client_module
    import src.graph.crm_deal_identity_repair_control as control_repository_module
    import src.graph.crm_deal_identity_repair_ledger as ledger_module
    import src.graph.crm_deal_identity_repair_ledger_migration as migration_module

    settings = SimpleNamespace(
        deployment_environment="staging", crm_deal_identity_repair_enabled=False
    )
    calls: list[str] = []

    class Ledger:
        def __init__(self, _client: object) -> None:
            calls.append("ledger")

        def get_qualification(self, repair_id: str) -> RepairQualificationRun:
            assert repair_id == "repair-1"
            calls.append("get_qualification")
            return _run()

        def source_record_pks(self, repair_id: str) -> tuple[str, ...]:
            assert repair_id == "repair-1"
            calls.append("source_record_pks")
            return ("pk-1",)

        def snapshot(
            self,
            *,
            source_instance_id: str,
            control_instance_id: str,
            source_record_pks: tuple[str, ...],
        ) -> object:
            assert (source_instance_id, control_instance_id, source_record_pks) == (
                "legacy-default",
                "legacy-default",
                ("pk-1",),
            )
            calls.append("snapshot")
            return "current-boundary"

        def get_status(self, repair_id: str, snapshot: object, reason: object) -> object:
            assert (repair_id, snapshot, reason) == ("repair-1", "current-boundary", None)
            calls.append("get_status")
            return SimpleNamespace(
                repair_id=repair_id,
                admissibility="admissible",
                reason_code="exact_boundary_match",
                manifest_digest=_DIGEST,
                qualification_identity=_DIGEST,
                expected_boundary_digest=_DIGEST,
                observed_boundary_digest=_DIGEST,
                source_instance_id="legacy-default",
                control_instance_id="legacy-default",
                inventory_row_count=1,
                eligible_unit_count=1,
                negative_control_count=0,
            )

    class Control:
        def __init__(self, _client: object) -> None:
            calls.append("control")

        def status(self, repair_id: str) -> RepairControlStatus:
            assert repair_id == "repair-1"
            calls.append("status")
            return RepairControlStatus(
                repair_id,
                "qualified",
                "paused",
                True,
                7,
                "quiesced",
                "allocated",
                "allocated",
                3,
            )

    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(client_module, "Neo4jClient", _Client)
    monkeypatch.setattr(ledger_module, "CrmDealRepairLedgerRepository", Ledger)
    monkeypatch.setattr(control_repository_module, "CrmDealRepairControlRepository", Control)
    monkeypatch.setattr(
        migration_module, "assert_crm_deal_repair_ledger_ready", lambda _client: None
    )

    assert main(_status_arguments()) == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [
        "ledger",
        "get_qualification",
        "source_record_pks",
        "snapshot",
        "get_status",
        "control",
        "status",
    ]
    assert payload == {
        "repair_id": "repair-1",
        "admissibility": "admissible",
        "reason_code": "exact_boundary_match",
        "manifest_digest": _DIGEST,
        "qualification_identity": _DIGEST,
        "expected_boundary_digest": _DIGEST,
        "observed_boundary_digest": _DIGEST,
        "source_instance_id": "legacy-default",
        "control_instance_id": "legacy-default",
        "inventory_row_count": 1,
        "eligible_unit_count": 1,
        "negative_control_count": 0,
        "control_state": "paused",
        "dispatch_blocked": True,
        "dispatch_revision": 7,
        "quiescence_state": "quiesced",
        "allocation_state": "allocated",
        "paused_from_state": "allocated",
        "allocated_unit_count": 3,
        "execution_allowed": False,
    }


def _install_allocate_seams(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overlay: object,
) -> list[tuple[object, ...]]:
    import sys

    import src.config as config_module
    import src.crm_deal_identity_repair.approval_overlay as overlay_module
    import src.graph.client as client_module

    artifacts_module = ModuleType("src.crm_deal_identity_repair.artifacts")
    qualification_module = ModuleType("src.crm_deal_identity_repair.qualification")
    monkeypatch.setitem(sys.modules, artifacts_module.__name__, artifacts_module)
    monkeypatch.setitem(sys.modules, qualification_module.__name__, qualification_module)
    import src.graph.crm_deal_identity_repair_control as control_repository_module
    import src.graph.crm_deal_identity_repair_ledger as ledger_module
    import src.graph.crm_deal_identity_repair_ledger_migration as migration_module

    run = _run()
    item = _item()
    inventory_path = tmp_path / "qualified-artifact"
    inventory_path.mkdir()
    (inventory_path / "inventory.jsonl").write_text(
        json.dumps(item.to_dict()) + "\n", encoding="utf-8"
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(provenance=SimpleNamespace(artifact_path=str(inventory_path)))
    )
    settings = SimpleNamespace(
        deployment_environment="staging",
        crm_deal_identity_repair_enabled=True,
        crm_deal_identity_repair_approval_key_secret=_Secret("approval-secret"),
        crm_deal_identity_repair_approval_key_id="approval-key-1",
        crm_deal_identity_repair_approval_root=str(tmp_path / "approvals"),
    )
    allocations: list[tuple[object, ...]] = []
    monkeypatch.setenv("CRM_DEAL_IDENTITY_REPAIR_CONTROL_TOKEN", "control-secret")

    class Ledger:
        def __init__(self, _client: object) -> None:
            pass

        def get_qualification(self, repair_id: str) -> RepairQualificationRun:
            assert repair_id == run.repair_id
            return run

    class Control:
        def __init__(self, _client: object) -> None:
            pass

        def proof_digest(self, request: object) -> str:
            allocations.append(("proof", request))
            return _DIGEST

        def allocate(
            self,
            request: object,
            *,
            boundary_digest: str,
            proof_digest: str,
            plan: object,
            allocation_origin_key_id: str,
            allocation_origin_secret: bytes,
        ) -> RepairDispatchLease:
            assert allocation_origin_key_id == "approval-key-1"
            assert allocation_origin_secret == b"approval-secret"
            allocations.append(("allocate", request, boundary_digest, proof_digest, plan))
            return RepairDispatchLease(
                "legacy-default",
                "run-1",
                "owner-1",
                control_token_digest("control-secret"),
                2,
                "allocated",
                _DIGEST,
            )

    monkeypatch.setattr(config_module, "get_settings", lambda: settings)
    monkeypatch.setattr(client_module, "Neo4jClient", _Client)
    monkeypatch.setattr(ledger_module, "CrmDealRepairLedgerRepository", Ledger)
    monkeypatch.setattr(control_repository_module, "CrmDealRepairControlRepository", Control)
    monkeypatch.setattr(
        migration_module, "assert_crm_deal_repair_ledger_ready", lambda _client: None
    )
    monkeypatch.setattr(
        artifacts_module,
        "repair_artifact_store_from_settings",
        lambda _settings: nullcontext(object()),
        raising=False,
    )
    monkeypatch.setattr(
        qualification_module,
        "verify_qualified_repair_artifact",
        lambda _store, *, run: verified if run == _run() else pytest.fail("wrong stored run"),
        raising=False,
    )

    def verify(path: Path, *, secret: bytes) -> ApprovalOverlay:
        assert path == tmp_path / "approvals" / "approval-1.json"
        assert secret == b"approval-secret"
        return cast(ApprovalOverlay, overlay)

    monkeypatch.setattr(overlay_module, "verify_approval_overlay", verify)
    return allocations


def test_allocate_uses_only_stored_qualified_artifact_and_remains_non_executable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Allocate accepts no qualification CLI inputs and derives rows from authenticated storage."""
    run = _run()
    allocations = _install_allocate_seams(monkeypatch, tmp_path, _overlay(run))

    assert main(_control_arguments()) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert "control-secret" not in output
    assert [entry[0] for entry in allocations] == ["proof", "allocate"]
    plan = cast(AllocationPlan, allocations[1][4])
    assert plan.completion.unit_count == 1
    assert plan.units[0].inventory_key == _item().inventory_key
    assert payload == {
        "repair_id": "repair-1",
        "run_id": "run-1",
        "state": "allocated",
        "revision": 2,
        "execution_allowed": False,
    }


_BINDING_MUTATIONS: tuple[tuple[str, object], ...] = (
    ("repair_id", "other-repair"),
    ("run_id", "other-run"),
    ("qualification_identity", f"sha256:{'e' * 64}"),
    ("artifact_id", "f" * 32),
    ("artifact_manifest_hmac", "f" * 64),
    ("inventory_digest", f"sha256:{'f' * 64}"),
    ("inventory_row_count", 2),
    ("boundary_digest", f"sha256:{'1' * 64}"),
    ("repository_sha", "e" * 40),
    ("image_digest", f"sha256:{'2' * 64}"),
    ("configuration_digest", f"sha256:{'3' * 64}"),
    ("source_contract_uuid", "12345678-1234-5678-9234-567812345679"),
    ("approval_reference", "other-approval"),
    ("unit_ceiling", 0),
    ("key_id", "other-key"),
)


@pytest.mark.parametrize(("field", "value"), _BINDING_MUTATIONS)
def test_allocate_rejects_each_changed_approval_overlay_or_key_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    run = _run()
    changed = SimpleNamespace(**(asdict(_overlay(run)) | {field: value}))
    allocations = _install_allocate_seams(monkeypatch, tmp_path, changed)

    with pytest.raises(RuntimeError, match="does not bind the qualified manifest exactly"):
        main(_control_arguments())

    assert allocations == []


def test_control_command_rejects_missing_or_persisted_digest_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only a raw secret may enter the control command credential boundary."""
    _install_allocate_seams(monkeypatch, tmp_path, _overlay(_run()))
    monkeypatch.delenv("CRM_DEAL_IDENTITY_REPAIR_CONTROL_TOKEN")
    with pytest.raises(RuntimeError, match="secret environment channel"):
        main(_control_arguments())

    monkeypatch.setenv(
        "CRM_DEAL_IDENTITY_REPAIR_CONTROL_TOKEN", control_token_digest("control-secret")
    )
    with pytest.raises(ValueError, match="not an operator secret"):
        main(_control_arguments())
