"""Focused pure contracts for #310 control, allocation, and absence evidence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.allocation import allocation_origin_hmac, plan_allocation
from src.crm_deal_identity_repair.approval_overlay import (
    APPROVAL_OVERLAY_HMAC_DOMAIN,
    APPROVAL_OVERLAY_VERSION,
    ApprovalOverlay,
    ApprovalRow,
    verify_approval_overlay,
)
from src.crm_deal_identity_repair.control_models import (
    CapturedTaskTopologyIdentity,
    RepairControlRequest,
    RepairDispatchLease,
    control_token_digest,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.task_inspection import (
    BrokerInspection,
    BrokerInspector,
    TaskAbsenceEvidence,
    WorkerInspector,
    collect_absence_evidence,
    verify_absence_evidence,
)

DIGEST = "sha256:" + "a" * 64
TOKEN_DIGEST = control_token_digest("token-1")


def _captured_tasks() -> tuple[CapturedTaskTopologyIdentity, ...]:
    return (CapturedTaskTopologyIdentity("control-1", "generation-1", "logical-1", 7),)


class _Workers:
    def inspect(self, timeout_seconds: int) -> dict[str, dict[str, tuple[dict[str, object], ...]]]:
        assert timeout_seconds == 10
        return {"worker-a": {"active": (), "reserved": (), "scheduled": ()}}


def _broker_topology() -> dict[str, object]:
    from src.crm_deal_identity_repair.task_inspection import _redis_priority_queue_names

    return {
        "ready_priority_keys": list(_redis_priority_queue_names("ingestion")),
        "unacked_hash": "unacked",
        "unacked_index": "unacked_index",
        "unacked_wrapper": "kombu-redis-json-v1",
    }


class _Broker:
    def inspect(self, selectors: tuple[str, ...]) -> BrokerInspection:
        return BrokerInspection(_broker_topology(), {"ready": (), "unacked": ()})


class _BrokerWithDelivery:
    def __init__(self, delivery: dict[str, object], inventory: str = "ready") -> None:
        self._delivery = delivery
        self._inventory = inventory

    def inspect(self, selectors: tuple[str, ...]) -> BrokerInspection:
        assert selectors
        return BrokerInspection(
            _broker_topology(),
            {
                "ready": (self._delivery,) if self._inventory == "ready" else (),
                "unacked": (self._delivery,) if self._inventory == "unacked" else (),
            },
        )


def _item(partition: str = "ownership_repair") -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="deal-1",
        source_record_pk="pk-1",
        deal_id="1",
        partition=partition,
        graph_fingerprint=DIGEST,
        stored_payload_fingerprint=DIGEST,
        payload={},
    )


def _overlay(disposition: str = "executable") -> ApprovalOverlay:
    item = _item()
    return ApprovalOverlay(
        "approval-1",
        "repair-1",
        "run-1",
        DIGEST,
        "artifact-1",
        DIGEST,
        DIGEST,
        1,
        DIGEST,
        "repo",
        "image",
        "config",
        "contract",
        "approval-ref",
        1,
        (ApprovalRow(item.inventory_key, item.source_record_pk, DIGEST, DIGEST, disposition),),
        "key-1",
        DIGEST,
    )


def test_control_request_requires_revision_and_allocation_is_deterministic() -> None:
    request = RepairControlRequest("repair-1", "run-1", "owner-1", "token-1", 0)
    assert request.expected_revision == 0
    first = plan_allocation(
        run_id="run-1", boundary_digest=DIGEST, inventory=(_item(),), overlay=_overlay()
    )
    second = plan_allocation(
        run_id="run-1", boundary_digest=DIGEST, inventory=(_item(),), overlay=_overlay()
    )
    assert first.units == second.units
    assert first.completion == second.completion


def test_allocation_origin_hmac_authenticates_the_allocation_time_boundary() -> None:
    def seal(secret: bytes, sealed_boundary_digest: str) -> str:
        return allocation_origin_hmac(
            secret=secret,
            key_id="key-1",
            control_instance_id="control-1",
            run_id="run-1",
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            revision=2,
            boundary_digest=DIGEST,
            sealed_boundary_digest=sealed_boundary_digest,
            completion_id="completion-1",
            overlay_digest=DIGEST,
            allocation_digest=DIGEST,
            unit_count=0,
            unit_set_digest=DIGEST,
            request_digest=DIGEST,
        )

    allocation_seal = seal(b"secret", "sealed-1")
    assert allocation_seal == seal(b"secret", "sealed-1")
    assert allocation_seal != seal(b"secret", "sealed-2")
    assert allocation_seal != seal(b"other-secret", "sealed-1")


def test_zero_unit_requires_complete_nonempty_overlay() -> None:
    plan = plan_allocation(
        run_id="run-1", boundary_digest=DIGEST, inventory=(_item(),), overlay=_overlay("blocked")
    )
    assert plan.units == ()
    assert plan.completion.unit_count == 0
    with pytest.raises(ValueError, match="non-empty"):
        plan_allocation(
            run_id="run-1", boundary_digest=DIGEST, inventory=(), overlay=_overlay("blocked")
        )


def test_control_request_and_task_evidence_never_retain_plaintext_token() -> None:
    request = RepairControlRequest("repair-1", "run-1", "owner-1", "plaintext-secret", 1)
    assert request.token_digest != "plaintext-secret"
    assert request.token_digest.startswith("sha256:")
    with pytest.raises(ValueError, match="not an operator secret"):
        RepairControlRequest("repair-1", "run-1", "owner-1", request.token_digest, 2)
    assert not hasattr(RepairControlRequest, "from_durable_token_digest")
    assert request.operator_secret == ""


def test_absence_evidence_is_signed_fresh_and_bound() -> None:
    evidence = collect_absence_evidence(
        worker=_Workers(),
        broker=_Broker(),
        run_id="run-1",
        captured_tasks=_captured_tasks(),
        boundary_digest=DIGEST,
        owner_id="owner-1",
        token_digest=TOKEN_DIGEST,
        dispatch_revision=1,
        topology_digest=DIGEST,
        expected_workers=("worker-a",),
        timeout_seconds=10,
        max_age_seconds=60,
        key_id="key-1",
        secret=b"secret",
        now=datetime.now(UTC),
    )
    assert evidence.token_digest.startswith("sha256:")
    assert evidence.token_digest == TOKEN_DIGEST
    assert "token-1" not in json.dumps(evidence.payload(), sort_keys=True)
    assert verify_absence_evidence(evidence, secret=b"secret", now=datetime.now(UTC))
    assert not verify_absence_evidence(evidence, secret=b"changed", now=datetime.now(UTC))


def test_broker_topology_is_signed_and_tampering_fails_authentication() -> None:
    evidence = collect_absence_evidence(
        worker=_Workers(),
        broker=_Broker(),
        run_id="run-1",
        captured_tasks=_captured_tasks(),
        boundary_digest=DIGEST,
        owner_id="owner-1",
        token_digest=TOKEN_DIGEST,
        dispatch_revision=1,
        topology_digest=DIGEST,
        expected_workers=("worker-a",),
        timeout_seconds=10,
        max_age_seconds=60,
        key_id="key-1",
        secret=b"secret",
        now=datetime.now(UTC),
    )
    assert verify_absence_evidence(evidence, secret=b"secret", now=datetime.now(UTC))
    object.__setattr__(
        evidence, "broker_topology", {**evidence.broker_topology, "unacked_index": "other"}
    )
    assert not verify_absence_evidence(evidence, secret=b"secret", now=datetime.now(UTC))


def _overlay_transport(*, hmac_value: str | None = None) -> dict[str, object]:
    row = _item()
    payload: dict[str, object] = {
        "version": APPROVAL_OVERLAY_VERSION,
        "approval_id": "approval-1",
        "repair_id": "repair-1",
        "run_id": "run-1",
        "qualification_identity": DIGEST,
        "artifact_id": "artifact-1",
        "artifact_manifest_hmac": DIGEST,
        "inventory_digest": DIGEST,
        "inventory_row_count": 1,
        "boundary_digest": DIGEST,
        "repository_sha": "repo",
        "image_digest": "image",
        "configuration_digest": "config",
        "source_contract_uuid": "contract",
        "approval_reference": "approval-ref",
        "unit_ceiling": 1,
        "rows": [
            {
                "inventory_key": row.inventory_key,
                "source_record_pk": row.source_record_pk,
                "graph_fingerprint": DIGEST,
                "stored_payload_fingerprint": DIGEST,
                "disposition": "executable",
            }
        ],
        "key_id": "key-1",
    }
    signature = (
        hmac_value
        or hmac.new(
            b"secret", APPROVAL_OVERLAY_HMAC_DOMAIN + canonical_json_bytes(payload), hashlib.sha256
        ).hexdigest()
    )
    return {**payload, "hmac": signature}


def test_approval_overlay_accepts_exact_canonical_transport_and_binding(tmp_path: Path) -> None:
    path = tmp_path / "overlay.json"
    path.write_bytes(canonical_json_bytes(_overlay_transport()))
    overlay = verify_approval_overlay(path, secret=b"secret")
    assert overlay.run_id == "run-1"
    assert verify_approval_overlay(path, secret=b"secret", expected=overlay) == overlay


@pytest.mark.parametrize("mutation", ("body", "hmac", "noncanonical", "binding"))
def test_approval_overlay_rejects_tampering_noncanonical_and_wrong_binding(
    tmp_path: Path, mutation: str
) -> None:
    path = tmp_path / "overlay.json"
    transport = _overlay_transport()
    expected: ApprovalOverlay | None = None
    if mutation == "body":
        transport["approval_reference"] = "tampered"
    elif mutation == "hmac":
        transport["hmac"] = "0" * 64
    elif mutation == "binding":
        expected = _overlay()
        transport["run_id"] = "other-run"
        payload = {key: value for key, value in transport.items() if key != "hmac"}
        transport["hmac"] = hmac.new(
            b"secret", APPROVAL_OVERLAY_HMAC_DOMAIN + canonical_json_bytes(payload), hashlib.sha256
        ).hexdigest()
    if mutation == "noncanonical":
        path.write_text(json.dumps(transport, indent=2), encoding="utf-8")
    else:
        path.write_bytes(canonical_json_bytes(transport))
    with pytest.raises(RuntimeError):
        verify_approval_overlay(path, secret=b"secret", expected=expected)


@pytest.mark.parametrize("inventory", ("ready", "unacked"))
def test_broker_affected_delivery_blocks_absence(inventory: str) -> None:
    delivery = {
        "name": "src.tasks.run_ingestion_task",
        "kwargs": _task_identity_payload(),
    }
    with pytest.raises(RuntimeError, match="broker delivery remains present"):
        collect_absence_evidence(
            worker=_Workers(),
            broker=_BrokerWithDelivery(delivery, inventory),
            run_id="run-1",
            captured_tasks=_captured_tasks(),
            boundary_digest=DIGEST,
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            dispatch_revision=1,
            topology_digest=DIGEST,
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            key_id="key-1",
            secret=b"secret",
            now=datetime.now(UTC),
        )


def test_broker_malformed_or_unbound_affected_delivery_fails_closed() -> None:
    malformed = {
        "name": "src.tasks.run_ingestion_task",
        "kwargs": {"control_instance_id": "control-1"},
    }
    with pytest.raises(RuntimeError, match="selector identity"):
        collect_absence_evidence(
            worker=_Workers(),
            broker=_BrokerWithDelivery(malformed),
            run_id="run-1",
            captured_tasks=_captured_tasks(),
            boundary_digest=DIGEST,
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            dispatch_revision=1,
            topology_digest=DIGEST,
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            key_id="key-1",
            secret=b"secret",
            now=datetime.now(UTC),
        )


def test_redis_unacked_wrapper_and_index_mismatch_fail_closed() -> None:
    from src.crm_deal_identity_repair.task_inspection import (
        _decode_unacked_delivery,
        _redis_unacked_inventory,
    )

    envelope = {
        "headers": {"task": "src.tasks.run_ingestion_task"},
        "body": base64.b64encode(json.dumps([[], _task_identity_payload(), {}]).encode()).decode(),
    }
    wrapped = json.dumps([json.dumps(envelope), "", "ingestion"])

    class Inventory:
        def lrange(self, name: str, start: int, end: int) -> list[object]:
            return []

        def hgetall(self, name: str) -> dict[object, object]:
            assert name == "unacked"
            return {"delivery-9": wrapped}

        def zrange(self, name: str, start: int, end: int) -> list[object]:
            assert name == "unacked_index"
            return ["delivery-9"]

    assert _decode_unacked_delivery(wrapped)["kwargs"]["attempt_generation"] == 7
    assert _redis_unacked_inventory(Inventory(), "unacked", "unacked_index")

    class MismatchedInventory(Inventory):
        def zrange(self, name: str, start: int, end: int) -> list[object]:
            return ["other"]

    with pytest.raises(RuntimeError, match="index/hash"):
        _redis_unacked_inventory(MismatchedInventory(), "unacked", "unacked_index")


def test_redis_inspector_reads_nonzero_priority_ready_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys
    from types import SimpleNamespace

    from src.crm_deal_identity_repair.task_inspection import RedisCeleryBrokerInspector

    envelope = {
        "headers": {"task": "src.tasks.run_ingestion_task"},
        "body": base64.b64encode(json.dumps([[], _task_identity_payload(), {}]).encode()).decode(),
    }

    class Inventory:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def lrange(self, name: str, start: int, end: int) -> list[object]:
            self.keys.append(name)
            return [json.dumps(envelope)] if name.endswith("9") else []

        def hgetall(self, name: str) -> dict[object, object]:
            return {}

        def zrange(self, name: str, start: int, end: int) -> list[object]:
            return []

    inventory = Inventory()
    monkeypatch.setitem(
        sys.modules,
        "redis",
        SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: inventory)),
    )
    inspection = RedisCeleryBrokerInspector("redis://broker").inspect(("selector",))
    assert inspection.observations["ready"][0]["kwargs"]["logical_run_id"] == "logical-1"
    assert "ingestion\x06\x169" in inventory.keys


def test_nonzero_priority_key_is_part_of_canonical_broker_topology() -> None:
    topology = _broker_topology()
    assert "ingestion\x06\x169" in topology["ready_priority_keys"]


def test_redis_celery_envelope_decoder_rejects_malformed_payload() -> None:
    from src.crm_deal_identity_repair.task_inspection import _decode_broker_delivery

    with pytest.raises(RuntimeError, match="not JSON"):
        _decode_broker_delivery(b"not-json")
    envelope = {
        "headers": {"task": "src.tasks.run_ingestion_task"},
        "body": base64.b64encode(b"[]").decode(),
    }
    with pytest.raises(RuntimeError, match="payload is malformed"):
        _decode_broker_delivery(canonical_json_bytes(envelope))


def test_complete_quiescence_query_requires_full_sealed_topology_and_empty_capture_is_safe() -> (
    None
):
    from src.graph.queries.crm_deal_identity_repair_control import (
        CLAIM_REPAIR_DISPATCH,
        COMPLETE_QUIESCENCE,
        READ_REPAIR_TOPOLOGY_SNAPSHOT,
    )

    assert "topology_json" in COMPLETE_QUIESCENCE
    assert "captured.checkpoint_ids" in COMPLETE_QUIESCENCE
    assert "captured.continuation_ids" in COMPLETE_QUIESCENCE
    assert "captured.fences" in COMPLETE_QUIESCENCE
    assert "all(publication IN $publications" in COMPLETE_QUIESCENCE
    assert "$stale_snapshot.state = 'orphan'" in COMPLETE_QUIESCENCE
    assert "$stale_snapshot.state = 'owned'" in COMPLETE_QUIESCENCE
    assert "CALL {\n  WITH control\n  UNWIND $captures" in COMPLETE_QUIESCENCE
    assert "RETURN count(stream) AS superseded_count" in COMPLETE_QUIESCENCE
    assert "FAIL_EXACT_REPAIR_STALE_RUN" not in COMPLETE_QUIESCENCE
    assert "existing.revision = $expected_revision + 1" in CLAIM_REPAIR_DISPATCH
    assert "WHEN existing.revision = $expected_revision THEN control.revision + 1" in (
        CLAIM_REPAIR_DISPATCH
    )
    assert "COUNT {" in COMPLETE_QUIESCENCE
    assert "[(checkpoint:IngestionCheckpoint" not in READ_REPAIR_TOPOLOGY_SNAPSHOT
    assert "[(stream:BitrixIngestionStream" not in READ_REPAIR_TOPOLOGY_SNAPSHOT


class _EmptyTopologyRepository:
    def __init__(self) -> None:
        from src.crm_deal_identity_repair.control_models import RepairDispatchLease

        self._lease = RepairDispatchLease(
            "control-1", "run-1", "owner-1", TOKEN_DIGEST, 1, "quiescing", DIGEST
        )
        self.stale_run_id: str | None = None

    def claim(
        self, request: RepairControlRequest, *, boundary_digest: str, control_instance_id: str
    ) -> RepairDispatchLease:
        assert (request.run_id, boundary_digest, control_instance_id) == (
            "run-1",
            DIGEST,
            "control-1",
        )
        return self._lease

    def request_stop_topology(
        self, *, control_instance_id: str, run_id: str, owner_id: str, stale_run_id: str
    ) -> str:
        assert (control_instance_id, run_id, owner_id) == ("control-1", "run-1", "owner-1")
        self.stale_run_id = stale_run_id
        return DIGEST

    def captured_task_identities(
        self, *, run_id: str, control_instance_id: str, topology_digest: str
    ) -> tuple[CapturedTaskTopologyIdentity, ...]:
        assert (run_id, control_instance_id, topology_digest) == ("run-1", "control-1", DIGEST)
        return ()

    def complete_quiescence(
        self,
        request: RepairControlRequest,
        *,
        boundary_digest: str,
        control_instance_id: str,
        topology_digest: str,
        evidence: TaskAbsenceEvidence,
        proof_secret: bytes,
        stale_run_id: str,
    ) -> RepairDispatchLease:

        assert request.expected_revision == 1
        assert (boundary_digest, control_instance_id, topology_digest) == (
            DIGEST,
            "control-1",
            DIGEST,
        )
        assert proof_secret == b"secret"
        assert stale_run_id == self.stale_run_id
        assert evidence is not None
        return RepairDispatchLease(
            "control-1", "run-1", "owner-1", TOKEN_DIGEST, 2, "quiesced", DIGEST
        )


def test_quiescence_service_rejects_a_zero_capture_boundary() -> None:
    from src.crm_deal_identity_repair.quiescence import RepairQuiescenceService
    from src.crm_deal_identity_repair.task_inspection import BrokerInspector, WorkerInspector

    repository = _EmptyTopologyRepository()
    with pytest.raises(ValueError, match="non-empty"):
        RepairQuiescenceService(
            repository, cast(WorkerInspector, _Workers()), cast(BrokerInspector, _Broker())
        ).quiesce(
            request=RepairControlRequest("repair-1", "run-1", "owner-1", "token-1", 0),
            boundary_digest=DIGEST,
            control_instance_id="control-1",
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            proof_key_id="key-1",
            proof_secret=b"secret",
            stale_run_id="sealed-stale-run",
        )
    assert repository.stale_run_id == "sealed-stale-run"


class _WorkersWithTask:
    def __init__(self, inventory: str, task: dict[str, object]) -> None:
        self._inventory = inventory
        self._task = task

    def inspect(self, timeout_seconds: int) -> dict[str, dict[str, tuple[dict[str, object], ...]]]:
        from src.crm_deal_identity_repair.task_inspection import _task_json

        assert timeout_seconds == 10
        observed = _task_json(self._task)
        return {
            "worker-a": {
                "active": (observed,) if self._inventory == "active" else (),
                "reserved": (observed,) if self._inventory == "reserved" else (),
                "scheduled": (observed,) if self._inventory == "scheduled" else (),
            }
        }


class _TopologyInventory:
    def __init__(self, inventory: str, task: dict[str, object]) -> None:
        self._inventory = inventory
        self._task = task

    def worker(self) -> WorkerInspector:
        worker_inventories = {"active", "reserved", "scheduled"}
        worker_inventory = self._inventory if self._inventory in worker_inventories else "active"
        task = self._task if self._inventory in worker_inventories else _unrelated_task()
        return cast(WorkerInspector, _WorkersWithTask(worker_inventory, task))

    def broker(self) -> BrokerInspector:
        if self._inventory in {"ready", "unacked"}:
            return cast(BrokerInspector, _BrokerWithDelivery(self._task, self._inventory))
        return cast(BrokerInspector, _Broker())


def _task_identity_payload(*, control_instance_id: str | None = "control-1") -> dict[str, object]:
    values: dict[str, object] = {
        "bitrix_generation_id": "generation-1",
        "logical_run_id": "logical-1",
        "attempt_generation": 7,
    }
    if control_instance_id is not None:
        values["control_instance_id"] = control_instance_id
    return values


def _unrelated_task() -> dict[str, object]:
    return {
        "name": "src.tasks.run_ingestion_task",
        "kwargs": {
            "control_instance_id": "other-control",
            "bitrix_generation_id": "other-generation",
            "logical_run_id": "other-logical",
            "attempt_generation": 8,
        },
    }


@pytest.mark.parametrize("inventory", ("active", "reserved", "scheduled", "ready", "unacked"))
def test_every_task_inventory_blocks_the_exact_captured_topology(inventory: str) -> None:
    task = {
        "name": "src.tasks.run_ingestion_task",
        "kwargs": _task_identity_payload(),
        "headers": {"trace": {"task": "repair"}},
    }
    with pytest.raises(RuntimeError, match="task or broker delivery remains present"):
        collect_absence_evidence(
            worker=_TopologyInventory(inventory, task).worker(),
            broker=_TopologyInventory(inventory, task).broker(),
            run_id="repair-run-is-not-generation-1",
            captured_tasks=_captured_tasks(),
            boundary_digest=DIGEST,
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            dispatch_revision=1,
            topology_digest=DIGEST,
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            key_id="key-1",
            secret=b"secret",
            now=datetime.now(UTC),
        )


def test_legacy_delivery_without_control_id_is_precise_or_fails_closed() -> None:
    legacy_topology = (
        CapturedTaskTopologyIdentity("legacy-default", "generation-1", "logical-1", 7),
    )
    legacy_delivery = {
        "name": "src.tasks.run_ingestion_task",
        "kwargs": _task_identity_payload(control_instance_id=None),
    }
    with pytest.raises(RuntimeError, match="delivery remains present"):
        collect_absence_evidence(
            worker=_Workers(),
            broker=_BrokerWithDelivery(legacy_delivery),
            run_id="repair-run-is-not-generation-1",
            captured_tasks=legacy_topology,
            boundary_digest=DIGEST,
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            dispatch_revision=1,
            topology_digest=DIGEST,
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            key_id="key-1",
            secret=b"secret",
            now=datetime.now(UTC),
        )
    with pytest.raises(RuntimeError, match="legacy task control selector is ambiguous"):
        collect_absence_evidence(
            worker=_Workers(),
            broker=_BrokerWithDelivery(legacy_delivery),
            run_id="repair-run-is-not-generation-1",
            captured_tasks=_captured_tasks(),
            boundary_digest=DIGEST,
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            dispatch_revision=1,
            topology_digest=DIGEST,
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            key_id="key-1",
            secret=b"secret",
            now=datetime.now(UTC),
        )


def test_worker_unrelated_and_malformed_nested_tasks_are_distinguished() -> None:
    unrelated = {
        "name": "src.tasks.run_ingestion_task",
        "kwargs": {
            "control_instance_id": "other-control",
            "bitrix_generation_id": "other-generation",
            "logical_run_id": "other-logical",
            "attempt_generation": 8,
        },
        "headers": {"origin": "other"},
    }
    evidence = collect_absence_evidence(
        worker=cast(WorkerInspector, _WorkersWithTask("active", unrelated)),
        broker=cast(BrokerInspector, _Broker()),
        run_id="run-1",
        captured_tasks=_captured_tasks(),
        boundary_digest=DIGEST,
        owner_id="owner-1",
        token_digest=TOKEN_DIGEST,
        dispatch_revision=1,
        topology_digest=DIGEST,
        expected_workers=("worker-a",),
        timeout_seconds=10,
        max_age_seconds=60,
        key_id="key-1",
        secret=b"secret",
        now=datetime.now(UTC),
    )
    assert evidence.observations["worker-a"][0]["kwargs"] == unrelated["kwargs"]
    assert evidence.observations["worker-a"][0]["headers"] == unrelated["headers"]
    malformed = {"name": "src.tasks.run_ingestion_task", "kwargs": ["not", "an", "object"]}
    with pytest.raises(RuntimeError, match="kwargs are malformed"):
        collect_absence_evidence(
            worker=cast(WorkerInspector, _WorkersWithTask("scheduled", malformed)),
            broker=cast(BrokerInspector, _Broker()),
            run_id="run-1",
            captured_tasks=_captured_tasks(),
            boundary_digest=DIGEST,
            owner_id="owner-1",
            token_digest=TOKEN_DIGEST,
            dispatch_revision=1,
            topology_digest=DIGEST,
            expected_workers=("worker-a",),
            timeout_seconds=10,
            max_age_seconds=60,
            key_id="key-1",
            secret=b"secret",
            now=datetime.now(UTC),
        )
