"""Cross-module cleanup durable-evidence integration tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from intelligence.artifacts import publish_inventory, scan_staged_outputs
from intelligence.crm.activities.acceptance import (
    parse_descriptor,
    publication_descriptor,
    write_publication_descriptor,
)
from intelligence.crm.activities.cleanup import admission, checkpoints
from intelligence.crm.activities.cleanup.commands import _run
from intelligence.crm.activities.cleanup.config import CleanupConfig
from intelligence.crm.activities.cleanup.models import (
    CleanupAuthorization as CleanupRequestAuthorization,
)
from intelligence.crm.activities.cleanup.models import CleanupRequest
from intelligence.crm.activities.cleanup.models import CleanupTarget as CleanupRequestTarget
from intelligence.crm.activities.cleanup.quiescence import _write_registration
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.status import read_status
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ProtectedSourceEndpointEvidence,
    QuiescenceEvidence,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.crm.activities.config import CrmActivitiesConfig
from intelligence.crm.activities.dispositions import classify
from intelligence.crm.activities.manifests import write_snapshot
from intelligence.crm.activities.models import ArchiveRecord, ArchiveRequest, ParentReference
from intelligence.crm.activities.reconciliation import seal
from intelligence.models import OutputInventory, Run
from intelligence.repositories.protocols.crm_activity_cleanup import (
    BatchOutcome,
    CleanupPlan,
    EndpointIdentity,
    ExactRecordInspection,
    ExpectedDeletionFact,
    IncidentRelationship,
    LiveTargetIdentity,
    ObservedRecord,
    ParentIdentity,
    RecordOutcome,
)
from intelligence.state import State


def test_checkpoint_accessor_and_status_share_exact_outcome_evidence(tmp_path: Path) -> None:
    authorization = CleanupAuthorization(
        "checkpoint-a",
        "archive-a",
        "snapshot-a",
        canonical_digest("manifest"),
        canonical_digest("boundary"),
        canonical_digest("cleanup"),
        "archive-db",
    )
    target = CleanupTarget("environment-a", "environment-a", "database-a", "database-a")
    quiescence = QuiescenceEvidence.create(
        "quiescence-a",
        authorization.accepted_run_id,
        authorization.checkpoint_id,
        authorization.logical_snapshot_id,
        authorization.manifest_digest,
        authorization.cleanup_identity_digest,
        "bitrix_chat",
        "bitrix-a",
        target.configured_environment_id,
        target.observed_database_identity,
        authorization.boundary_digest,
        "test-operator",
        "test-reference",
    )
    receipt = CleanupReceipt.create(
        "cleanup-a",
        authorization,
        target,
        1,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        quiescence,
        {"present_identity_count": 1},
        (
            CleanupIdentity(
                "source-a",
                "crm_history",
                "bitrix-a",
                "v1",
                "hash-a",
                canonical_digest("record"),
                canonical_digest("reference"),
                0,
                canonical_digest("relationship"),
                0,
                canonical_digest("dependency"),
            ),
        ),
        protected_source_endpoints=(
            ProtectedSourceEndpointEvidence(
                "source-a",
                "relationship-a",
                "FROM_SOURCE",
                "outbound",
                "source-system-a",
                ("SourceSystem",),
                "bitrix_chat",
            ),
        ),
    )
    root = checkpoints.checkpoint_root(tmp_path, "cleanup-a")
    checkpoint = checkpoints.initialize(root, "cleanup-a", receipt)
    checkpoints.record_attempt(root, checkpoint, "attempt-a")
    checkpoints.write_batch_intent(root, checkpoint, 1, ("source-a",))
    checkpoint = checkpoints.write_batch_result(
        root, checkpoint, 1, {"source-a": "conflict"}, {"source-a": "revalidation_mismatch"}
    )
    durable = checkpoints.durable_outcomes(root, checkpoint)
    assert durable.outcomes == (("source-a", "conflict"),)
    assert durable.failure_codes == (("source-a", "revalidation_mismatch"),)
    assert dict(read_status(tmp_path, "cleanup-a").outcome_counts)["conflict"] == 1


def _publish(state: State, command: str, writer: object) -> tuple[Run, tuple[OutputInventory, ...]]:
    run = state.create_mutating_run(command)
    staging = state.layout.staging / run.run_id
    staging.mkdir(parents=True)
    assert callable(writer)
    writer(staging)
    inventory = scan_staged_outputs(state.workspace, run.run_id, 10_000_000, 100)
    state.mark_execution_quiescent(run)
    state.begin_publishing(run, inventory)
    published = publish_inventory(state.workspace, run.run_id, inventory, 10_000_000, 100)
    state.complete_publication(run, published, {"run_id": run.run_id, "command": command})
    return run, published


def _archive_record() -> ArchiveRecord:
    return ArchiveRecord(
        "activity-a",
        "record-a",
        "1",
        "version-a",
        "hash-a",
        "bitrix-primary",
        "bitrix_chat",
        "crm_history",
        "active",
        "activity",
        "call",
        "bitrix_crm_activity",
        "2",
        "bitrix_crm_activity_v2",
        None,
        "2026-09-08T00:00:00Z",
        None,
        ParentReference(None, "bitrix-primary", "deal-a", "crm_deal", "STORED_PARENT"),
        (),
        (),
        (),
        (),
    )


@dataclass
class _FlowRepository:
    present: bool = True
    lose_ack_once: bool = True

    def database_identity(self) -> str:
        return "database-a"

    def inspect(self, keys: tuple[str, ...]) -> tuple[ExactRecordInspection, ...]:
        if not self.present:
            return tuple(ExactRecordInspection(key, 0, None, (), 0, False) for key in keys)
        return tuple(self._present(identity_key) for identity_key in keys)

    def _present(self, key: str) -> ExactRecordInspection:
        observed = ObservedRecord(
            "node-activity-a",
            ("SourceRecord",),
            "crm_history",
            "bitrix-primary",
            "1",
            "hash-a",
            "active",
            "activity",
            ParentIdentity(None, "bitrix-primary", "deal-a", "crm_deal", "STORED_PARENT"),
            source_record_id="record-a",
            source_version_key="version-a",
            history_source="bitrix_crm_activity",
            projection_source="bitrix_crm_activity_v2",
            projection_version="2",
        )
        relationship = IncidentRelationship(
            "from-source-a",
            "outbound",
            "FROM_SOURCE",
            EndpointIdentity(
                "source-system-a",
                ("SourceSystem",),
                None,
                None,
                None,
                None,
                "bitrix_chat",
                None,
                None,
            ),
        )
        return ExactRecordInspection(key, 1, observed, (relationship,), 1, False)

    def plan(
        self,
        identities: tuple[LiveTargetIdentity, ...],
        inspections: tuple[ExactRecordInspection, ...],
    ) -> CleanupPlan:
        expected: list[ExpectedDeletionFact] = []
        outcomes: list[RecordOutcome] = []
        for identity, inspection in zip(identities, inspections, strict=True):
            if inspection.record is None:
                outcomes.append(
                    RecordOutcome(
                        identity.source_record_pk,
                        "already_absent",
                        "absent_before_mutation",
                    )
                )
            else:
                expected.append(
                    ExpectedDeletionFact(
                        identity,
                        inspection.record,
                        inspection.incident_relationships,
                        ("from-source-a",),
                    )
                )
                outcomes.append(
                    RecordOutcome(identity.source_record_pk, "retained", "ready_for_batch_mutation")
                )
        return CleanupPlan(tuple(expected), (), tuple(outcomes))

    def delete_batch(
        self, database_identity: str, expected: tuple[ExpectedDeletionFact, ...]
    ) -> BatchOutcome:
        return self.delete_batch_with_required_absences(database_identity, expected, ())

    def delete_batch_with_required_absences(
        self,
        database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
        required_absent: tuple[LiveTargetIdentity, ...],
    ) -> BatchOutcome:
        assert database_identity == "database-a"
        if expected and self.lose_ack_once:
            self.present = False
            self.lose_ack_once = False
            raise RuntimeError("simulated acknowledgement loss")
        return BatchOutcome(
            database_identity,
            tuple(
                sorted(
                    (
                        *(
                            RecordOutcome(item.target.source_record_pk, "deleted", "deleted")
                            for item in expected
                        ),
                        *(
                            RecordOutcome(
                                item.source_record_pk,
                                "already_absent",
                                "absent_at_mutation",
                            )
                            for item in required_absent
                        ),
                    ),
                    key=lambda item: item.source_record_pk,
                )
            ),
            bool(expected),
        )

    def verify_protected(self, _protected: tuple[object, ...]) -> tuple[object, ...]:
        return ()

    def verify_protected_source_endpoints(
        self, _endpoints: tuple[ProtectedSourceEndpointEvidence, ...]
    ) -> tuple[ProtectedSourceEndpointEvidence, ...]:
        return ()

    def close(self) -> None:
        return None


def test_real_archive_state_flow_recovers_lost_ack_and_verifies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = State(tmp_path)
    archive_request = ArchiveRequest(
        "checkpoint-a", "bitrix-primary", database_identity="database-a"
    )
    record = _archive_record()
    boundary = seal((record,), archive_request)
    published: dict[str, object] = {}

    def write_archive(staging: Path) -> None:
        manifest = write_snapshot(staging, boundary, (record,), classify((record,)))
        snapshot_inventory = scan_staged_outputs(tmp_path, staging.name, 10_000_000, 100)
        descriptor = publication_descriptor(
            archive_request.snapshot_id,
            archive_request,
            boundary.digest,
            staging.name,
            "crm_activities_extract",
            boundary.logical_snapshot_id,
            str(manifest["digest"]),
            str(manifest["cleanup_identity_digest"]),
            snapshot_inventory,
        )
        pointer = write_publication_descriptor(staging, descriptor)
        published.update(
            descriptor=parse_descriptor(descriptor), pointer=pointer, manifest=manifest
        )

    archive_run, _ = _publish(state, "crm_activities_extract", write_archive)
    descriptor = published["descriptor"]
    pointer = published["pointer"]
    manifest = published["manifest"]
    monkeypatch.setattr(
        admission, "accepted_publication_for_run", lambda *_args: (descriptor, pointer)
    )
    request = CleanupRequest(
        CleanupRequestAuthorization(
            archive_request.snapshot_id,
            archive_run.run_id,
            boundary.logical_snapshot_id,
            str(manifest["digest"]),
        ),
        CleanupRequestTarget("environment-a", "database-a"),
        1,
    )
    template = QuiescenceEvidence.create(
        "template",
        archive_run.run_id,
        archive_request.snapshot_id,
        boundary.logical_snapshot_id,
        str(manifest["digest"]),
        str(manifest["cleanup_identity_digest"]),
        archive_request.source_key,
        archive_request.source_instance_id,
        "environment-a",
        "database-a",
        boundary.digest,
        "test-operator",
        "test-reference",
    )
    quiescence_run, _ = _publish(
        state,
        "crm_activities_cleanup_quiescence",
        lambda staging: _write_registration(template, staging, lambda: False),
    )
    quiescence = QuiescenceEvidence.parse(
        json.loads(
            (
                tmp_path
                / "outputs"
                / quiescence_run.run_id
                / "quiescence"
                / "crm"
                / "activities"
                / f"{quiescence_run.run_id}.json"
            ).read_text()
        )
    )
    archive_config = CrmActivitiesConfig(
        "bolt://unused",
        "unused",
        "unused",
        None,
        "bitrix-primary",
        "bitrix_chat",
        100,
        100,
        10,
        1_000_000,
        100,
        "database-a",
        100,
    )
    config = CleanupConfig(archive_config, "environment-a", True)
    repository = _FlowRepository()

    receipt_box: dict[str, CleanupReceipt] = {}

    def write_dry_run(staging: Path) -> None:
        receipt = _run(
            "dry-run",
            request,
            config,
            state,
            repository,
            staging,
            lambda: False,
            None,
            None,
            "cleanup-flow",
            quiescence_run.run_id,
            quiescence.evidence_digest,
        )
        assert receipt is not None
        receipt_box["receipt"] = receipt

    receipt_run, _ = _publish(state, "crm_activities_cleanup_dry_run", write_dry_run)
    receipt = receipt_box["receipt"]
    execute_staging = state.layout.staging / "execute-attempt"
    execute_staging.mkdir()
    with pytest.raises(RuntimeError, match="acknowledgement loss"):
        _run(
            "execute",
            request,
            config,
            state,
            repository,
            execute_staging,
            lambda: False,
            receipt_run.run_id,
            receipt.logical_digest,
            "cleanup-flow",
            quiescence_run.run_id,
            quiescence.evidence_digest,
        )
    assert (
        checkpoints.unresolved_batch(
            checkpoints.checkpoint_root(tmp_path, "cleanup-flow", create=False),
            checkpoints.load(
                checkpoints.checkpoint_root(tmp_path, "cleanup-flow", create=False),
                "cleanup-flow",
                receipt,
            ),
        )
        == 1
    )
    for operation in ("resume", "verify"):
        staging = state.layout.staging / f"{operation}-attempt"
        staging.mkdir()
        _run(
            operation,
            request,
            config,
            state,
            repository,
            staging,
            lambda: False,
            receipt_run.run_id,
            receipt.logical_digest,
            "cleanup-flow",
            quiescence_run.run_id,
            quiescence.evidence_digest,
        )
    checkpoint = checkpoints.load(
        checkpoints.checkpoint_root(tmp_path, "cleanup-flow", create=False),
        "cleanup-flow",
        receipt,
    )
    assert checkpoint.phase == "reconciled"
    assert not repository.present
    assert tuple((state.layout.staging / "verify-attempt").rglob("*.json"))
    state.close()
