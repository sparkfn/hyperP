"""Adversarial receipt admission and deterministic logical receipt tests."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup.receipt import (
    CleanupReceipt,
    admit_published_receipt,
    receipt_relative_path,
)
from intelligence.crm.activities.cleanup.types import (
    AuthorizedCompanionRelationship,
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ProtectedSourceEndpointEvidence,
    QuiescenceEvidence,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.models import OutputInventory, Run


def _digest(name: str) -> str:
    return canonical_digest({"name": name})


def _receipt() -> CleanupReceipt:
    authorization = CleanupAuthorization(
        "checkpoint-a",
        "archive-run",
        "snapshot-a",
        _digest("manifest"),
        _digest("boundary"),
        _digest("cleanup"),
        "archive-db-fingerprint",
    )
    target = CleanupTarget("test-environment", "test-environment", "neo4j-a", "neo4j-a")
    identity = CleanupIdentity(
        "source-a",
        "crm_history",
        "bitrix-a",
        "v1",
        "hash-a",
        _digest("record"),
        _digest("references"),
        2,
        _digest("relationships"),
        1,
        _digest("dependencies"),
    )
    quiescence = QuiescenceEvidence.create(
        "quiescence-run",
        authorization.accepted_run_id,
        authorization.checkpoint_id,
        authorization.logical_snapshot_id,
        authorization.manifest_digest,
        authorization.cleanup_identity_digest,
        "bitrix-source",
        identity.source_instance_id,
        target.configured_environment_id,
        target.observed_database_identity,
        authorization.boundary_digest,
        "test-operator",
        "test-reference",
    )
    endpoint = ProtectedSourceEndpointEvidence(
        identity.source_record_pk,
        "from-source-a",
        "FROM_SOURCE",
        "outbound",
        "source-system-a",
        ("SourceSystem",),
        quiescence.source_key,
    )
    return CleanupReceipt.create(
        "cleanup-a",
        authorization,
        target,
        10,
        ResourceCeilings(100_000, 100, 20, 100),
        "policy-v1",
        quiescence,
        {"deals": 3, "persons": 4, "present_identity_count": 1},
        (identity,),
        protected_source_endpoints=(endpoint,),
    )


def _companion_receipt() -> CleanupReceipt:
    receipt = _receipt()
    activity = receipt.identities[0]
    call = CleanupIdentity(
        "call-a",
        "call",
        activity.source_instance_id,
        activity.source_record_version,
        "call-hash",
        _digest("call-record"),
        _digest("call-references"),
        2,
        _digest("call-relationships"),
        2,
        _digest("call-dependencies"),
    )
    return CleanupReceipt.create(
        receipt.cleanup_run_id,
        receipt.authorization,
        receipt.target,
        receipt.batch_size,
        receipt.resource_ceilings,
        receipt.policy_version,
        QuiescenceEvidence.create(
            receipt.quiescence_run_id,
            receipt.authorization.accepted_run_id,
            receipt.authorization.checkpoint_id,
            receipt.authorization.logical_snapshot_id,
            receipt.authorization.manifest_digest,
            receipt.authorization.cleanup_identity_digest,
            receipt.quiescence_source_key,
            receipt.quiescence_source_instance_id,
            receipt.target.configured_environment_id,
            receipt.target.observed_database_identity,
            receipt.authorization.boundary_digest,
            "test-operator",
            "test-reference",
        ),
        {**dict(receipt.protected_baseline), "present_identity_count": 2},
        (activity, call),
        protected_source_endpoints=(
            ProtectedSourceEndpointEvidence(
                call.source_record_pk,
                "from-source-call-a",
                "FROM_SOURCE",
                "outbound",
                "source-system-a",
                ("SourceSystem",),
                receipt.quiescence_source_key,
            ),
            ProtectedSourceEndpointEvidence(
                activity.source_record_pk,
                "from-source-activity-a",
                "FROM_SOURCE",
                "outbound",
                "source-system-a",
                ("SourceSystem",),
                receipt.quiescence_source_key,
            ),
        ),
        authorized_companion_relationships=(
            AuthorizedCompanionRelationship(
                "child-edge-a",
                "CHILD_OF",
                call.source_record_pk,
                activity.source_record_pk,
                "outbound",
                "inbound",
            ),
            AuthorizedCompanionRelationship(
                "details-edge-a",
                "DETAILS_HISTORY_ITEM",
                call.source_record_pk,
                activity.source_record_pk,
                "outbound",
                "inbound",
            ),
        ),
    )


class _State:
    def __init__(self, run: Run, outputs: tuple[OutputInventory, ...]) -> None:
        self._run = run
        self._outputs = outputs

    def inspect(self, run_id: str) -> Run | None:
        return self._run if run_id == self._run.run_id else None

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]:
        return self._outputs if run_id == self._run.run_id else ()


def _state_and_artifact(tmp_path: Path, receipt: CleanupReceipt) -> tuple[_State, str]:
    run_id = "receipt-run"
    relative = receipt_relative_path(run_id)
    path = tmp_path / "outputs" / run_id / relative
    path.parent.mkdir(parents=True)
    raw = canonical_json(receipt.as_dict()).encode("utf-8")
    path.write_bytes(raw)
    run = Run(run_id, "crm_activities_cleanup_dry_run", "completed", 1, 1.0, 1.0)
    inventory = OutputInventory(f"outputs/{run_id}/{relative}", sha256(raw).hexdigest(), len(raw))
    return _State(run, (inventory,)), run_id


def test_receipt_is_deterministic_across_runtime_attempts() -> None:
    first = _receipt()
    second = _receipt()
    assert first.as_dict() == second.as_dict()
    assert "receipt-run" not in canonical_json(first.as_dict())


def test_authorized_companion_relationships_round_trip_deterministically() -> None:
    first = _companion_receipt()
    second = _companion_receipt()
    assert first.as_dict() == second.as_dict()
    assert CleanupReceipt.parse(first.as_dict()) == first


def test_receipt_orders_companion_calls_before_activities() -> None:
    receipt = _receipt()
    activity = receipt.identities[0]
    call = CleanupIdentity(
        "zz-call",
        "call",
        activity.source_instance_id,
        activity.source_record_version,
        activity.record_hash,
        activity.accepted_record_digest,
        activity.reference_fingerprint,
        activity.incident_relationship_count,
        activity.incident_relationship_digest,
        activity.dependency_count,
        activity.dependency_digest,
    )
    reordered = CleanupReceipt.create(
        receipt.cleanup_run_id,
        receipt.authorization,
        receipt.target,
        receipt.batch_size,
        receipt.resource_ceilings,
        receipt.policy_version,
        QuiescenceEvidence.create(
            receipt.quiescence_run_id,
            receipt.authorization.accepted_run_id,
            receipt.authorization.checkpoint_id,
            receipt.authorization.logical_snapshot_id,
            receipt.authorization.manifest_digest,
            receipt.authorization.cleanup_identity_digest,
            receipt.quiescence_source_key,
            receipt.quiescence_source_instance_id,
            receipt.target.configured_environment_id,
            receipt.target.observed_database_identity,
            receipt.authorization.boundary_digest,
            "test-operator",
            "test-reference",
        ),
        {**dict(receipt.protected_baseline), "present_identity_count": 2},
        (activity, call),
        protected_source_endpoints=(
            ProtectedSourceEndpointEvidence(
                call.source_record_pk,
                "from-source-call-a",
                "FROM_SOURCE",
                "outbound",
                "source-system-a",
                ("SourceSystem",),
                receipt.quiescence_source_key,
            ),
            ProtectedSourceEndpointEvidence(
                activity.source_record_pk,
                "from-source-activity-a",
                "FROM_SOURCE",
                "outbound",
                "source-system-a",
                ("SourceSystem",),
                receipt.quiescence_source_key,
            ),
        ),
    )
    assert [item.record_type for item in reordered.identities] == ["call", "crm_history"]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("relationship_element_id", "other-edge"),
        ("call_source_record_pk", "unknown-call"),
        ("activity_source_record_pk", "unknown-activity"),
        ("relationship_type", "DETAILS_HISTORY_ITEM"),
        ("call_direction", "inbound"),
        ("activity_direction", "outbound"),
    ),
)
def test_authorized_companion_relationship_tampering_fails_closed(field: str, value: str) -> None:
    receipt = _companion_receipt()
    payload = receipt.as_dict()
    companions = payload["authorized_companion_relationships"]
    assert isinstance(companions, list) and isinstance(companions[0], dict)
    companions[0][field] = value
    with pytest.raises(ValueError):
        CleanupReceipt.parse(payload)


def test_authorized_companion_relationship_digest_and_sensitive_values_are_not_serialized() -> None:
    receipt = _companion_receipt()
    payload = receipt.as_dict()
    serialized = canonical_json(payload)
    assert "SENSITIVE-IDENTIFIER-VALUE" not in serialized
    payload["authorized_companion_relationship_digest"] = "0" * 64
    with pytest.raises(ValueError, match="authorized companion relationship digest"):
        CleanupReceipt.parse(payload)


@pytest.mark.parametrize("mutation", ("sha", "bytes", "schema", "digest", "command", "extra"))
def test_state_registered_receipt_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    receipt = _receipt()
    state, run_id = _state_and_artifact(tmp_path, receipt)
    if mutation == "sha":
        state._outputs = (
            OutputInventory(
                state._outputs[0].relative_path, _digest("wrong"), state._outputs[0].byte_count
            ),
        )
    elif mutation == "bytes":
        state._outputs = (
            OutputInventory(state._outputs[0].relative_path, state._outputs[0].sha256, 1),
        )
    elif mutation == "schema":
        path = tmp_path / "outputs" / run_id / receipt_relative_path(run_id)
        value = receipt.as_dict()
        value["unexpected"] = True
        path.write_bytes(canonical_json(value).encode("utf-8"))
    elif mutation == "digest":
        with pytest.raises(RuntimeError):
            admit_published_receipt(tmp_path, state, run_id, _digest("wrong"))
        return
    elif mutation == "command":
        state._run = Run(run_id, "other", "completed", 1, 1.0, 1.0)
    else:
        state._outputs = state._outputs + (
            OutputInventory("outputs/receipt-run/other.json", _digest("other"), 1),
        )
    with pytest.raises((RuntimeError, ValueError)):
        admit_published_receipt(tmp_path, state, run_id, receipt.logical_digest)


def test_state_registered_receipt_admits_exact_canonical_artifact(tmp_path: Path) -> None:
    receipt = _receipt()
    state, run_id = _state_and_artifact(tmp_path, receipt)
    assert admit_published_receipt(tmp_path, state, run_id, receipt.logical_digest) == receipt


def test_endpoint_evidence_covers_only_exactly_present_receipt_identities() -> None:
    present = _receipt()
    absent = CleanupIdentity(
        "source-absent",
        "crm_history",
        "bitrix-a",
        "v1",
        "hash-absent",
        _digest("record-absent"),
        _digest("refs-absent"),
        0,
        _digest("rels-absent"),
        0,
        _digest("deps-absent"),
    )

    def quiescence() -> QuiescenceEvidence:
        return QuiescenceEvidence.create(
            present.quiescence_run_id,
            present.authorization.accepted_run_id,
            present.authorization.checkpoint_id,
            present.authorization.logical_snapshot_id,
            present.authorization.manifest_digest,
            present.authorization.cleanup_identity_digest,
            present.quiescence_source_key,
            present.quiescence_source_instance_id,
            present.target.configured_environment_id,
            present.target.observed_database_identity,
            present.authorization.boundary_digest,
            "test-operator",
            "test-reference",
        )

    mixed = CleanupReceipt.create(
        "cleanup-mixed",
        present.authorization,
        present.target,
        2,
        present.resource_ceilings,
        present.policy_version,
        quiescence(),
        {**dict(present.protected_baseline), "present_identity_count": 1},
        (*present.identities, absent),
        protected_source_endpoints=present.protected_source_endpoints,
    )
    assert tuple(item.selected_source_record_pk for item in mixed.protected_source_endpoints) == (
        "source-a",
    )
    all_absent = CleanupReceipt.create(
        "cleanup-absent",
        present.authorization,
        present.target,
        1,
        present.resource_ceilings,
        present.policy_version,
        quiescence(),
        {"present_identity_count": 0},
        (absent,),
    )
    assert all_absent.protected_source_endpoints == ()
