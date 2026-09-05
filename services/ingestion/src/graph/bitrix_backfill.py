"""Neo4j repository for Bitrix corrective-generation evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import cast

from neo4j import ManagedTransaction, Record

from src.bitrix_backfill_models import (
    BackfillInventoryManifest,
    CoverageEntry,
    CoverageReconciliation,
    GenerationChildRun,
    GenerationProvenance,
    GenerationState,
    GenerationStatus,
    KnownOwnerMembershipSet,
    QualificationResult,
    TailVerification,
)
from src.bitrix_ingestion_models import BitrixStreamKey, FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.queries.bitrix_backfill import (
    ACTIVATE_BITRIX_SUCCESSOR_GENERATION,
    ALLOCATE_BITRIX_BACKFILL_GENERATION,
    ALLOCATE_BITRIX_SUCCESSOR_GENERATION,
    ATTACH_BACKFILL_LOGICAL_RUN,
    CAS_BITRIX_BACKFILL_GENERATION_STATUS,
    COMPLETE_BITRIX_BACKFILL_FREEZE,
    CONFIRM_BITRIX_SUCCESSOR_PUBLICATION,
    EXPORT_FROZEN_OWNER_COVERAGE,
    FREEZE_BITRIX_BACKFILL_GENERATION,
    GET_BITRIX_BACKFILL_GENERATION,
    GET_BITRIX_BACKFILL_INVENTORY,
    GET_BITRIX_COVERAGE_RECONCILIATION,
    GET_BITRIX_GENERATION_CATEGORY_MAPPING,
    GET_BITRIX_SUCCESSOR_PUBLICATION_OCCURRENCE,
    GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION,
    GET_KNOWN_OWNER_SET,
    GET_MAX_BITRIX_RESUME_WORKER_GENERATION,
    LIST_BITRIX_GENERATION_LOGICAL_RUNS,
    LIST_KNOWN_OWNER_IDS,
    LIST_KNOWN_OWNER_MEMBERS_PAGE,
    PREPARE_KNOWN_OWNER_SET,
    RECORD_BITRIX_BACKFILL_RECONCILIATION,
    RECORD_BITRIX_QUALIFICATION,
    REGISTER_BITRIX_BACKFILL_INVENTORY,
    REJECT_BITRIX_BACKFILL_GENERATION,
    SEAL_KNOWN_OWNER_SET,
    SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR,
    UPSERT_BITRIX_BACKFILL_COVERAGE,
    UPSERT_KNOWN_OWNER_MEMBERS,
    VERIFY_BITRIX_SUCCESSOR_TAIL,
)
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID, effective_control_instance_id


@dataclass(frozen=True)
class FrozenOwnerRow:
    deal_id: str
    category_id: str
    stage_id: str | None
    source_observation_hash: str


@dataclass(frozen=True)
class FrozenOwnerExport:
    generation_id: str
    source_contract_uuid: str
    configuration_digest: str
    image_digest: str
    boundary_digest: str
    owner_set_digest: str
    rows: tuple[FrozenOwnerRow, ...]
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID


_KNOWN_OWNER_BATCH_SIZE = 1000


class BitrixBackfillRepository:
    """Read frozen coverage and later manage corrective-generation state."""

    def __init__(
        self,
        client: Neo4jClient,
        control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID,
    ) -> None:
        self._client = client
        self._control_instance_id = effective_control_instance_id(control_instance_id)

    def _require_fence_control(self, fence_context: FenceContext) -> None:
        if fence_context.control_instance_id != self._control_instance_id:
            raise ValueError("fence control_instance_id does not match the backfill repository")

    def allocate_generation(
        self,
        generation_id: str,
        provenance: GenerationProvenance,
    ) -> bool:
        if not generation_id.strip():
            raise ValueError("generation_id must be non-empty")
        creation_token = uuid.uuid4().hex

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                ALLOCATE_BITRIX_BACKFILL_GENERATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                repository_sha=provenance.repository_sha,
                image_digest=provenance.image_digest,
                configuration_digest=provenance.configuration_digest,
                source_contract_uuid=provenance.source_contract_uuid,
                boundary_digest=provenance.boundary_digest,
                creation_token=creation_token,
            ).single()
            if record is None:
                raise RuntimeError("generation identity conflicts with existing provenance")
            return record["created"] is True

        return self._client.execute_write(_work)

    def register_inventory(
        self,
        generation_id: str,
        manifest: BackfillInventoryManifest,
    ) -> None:
        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                REGISTER_BITRIX_BACKFILL_INVENTORY,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                inventory_digest=manifest.digest,
                manifest_json=manifest.canonical_json,
                executed_stream_keys=[entry.stream_key for entry in manifest.executable_entries],
                reviewed_by=manifest.reviewed_by,
                backup_id=manifest.backup_id,
                backup_restore_evidence_digest=manifest.backup_restore_evidence_digest,
                minimum_fence_image_digest=manifest.minimum_fence_image_digest,
            ).single()
            if record is None:
                raise RuntimeError("generation rejected its reviewed inventory")

        self._client.execute_write(_work)

    def get_generation(self, generation_id: str) -> GenerationState:
        def _read(tx: ManagedTransaction) -> GenerationState:
            record = tx.run(
                GET_BITRIX_BACKFILL_GENERATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
            ).single()
            if record is None:
                raise RuntimeError("Bitrix generation does not exist")
            status = _required_str(record["status"], "generation_status")
            if status not in {
                "allocated",
                "backfilling",
                "reconciling",
                "frozen",
                "qualified",
                "accepted",
                "failed",
                "rejected",
                "superseded",
                "active",
                "activating",
            }:
                raise RuntimeError("Bitrix generation has an invalid status")
            return GenerationState(
                generation_id=generation_id,
                status=cast(GenerationStatus, status),
                generation_kind=_required_str(record["generation_kind"], "generation_kind"),
                inventory_digest=_optional_str(record["inventory_digest"]),
                corrective_generation_id=_optional_str(record["corrective_generation_id"]),
                frozen_at=_optional_str(record["frozen_at"]),
                material_write_count=_non_negative_int(record, "material_write_count"),
                repository_sha=_required_str(record["repository_sha"], "repository_sha"),
                image_digest=_required_str(record["image_digest"], "image_digest"),
                configuration_digest=_required_str(
                    record["configuration_digest"], "configuration_digest"
                ),
                boundary_digest=_required_str(record["boundary_digest"], "boundary_digest"),
                source_contract_uuid=_required_str(
                    record["source_contract_uuid"], "source_contract_uuid"
                ),
                control_instance_id=self._control_instance_id,
            )

        return self._client.execute_read(_read)

    def get_inventory_json(self, generation_id: str) -> tuple[str, str]:
        def _read(tx: ManagedTransaction) -> tuple[str, str]:
            record = tx.run(
                GET_BITRIX_BACKFILL_INVENTORY,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
            ).single()
            if record is None:
                raise RuntimeError("Bitrix generation has no accepted inventory")
            return (
                _required_str(record["manifest_json"], "manifest_json"),
                _required_str(record["inventory_digest"], "inventory_digest"),
            )

        return self._client.execute_read(_read)

    def get_max_resume_worker_generation(self, generation_id: str) -> int:
        def _read(tx: ManagedTransaction) -> int:
            record = tx.run(
                GET_MAX_BITRIX_RESUME_WORKER_GENERATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
            ).single()
            if record is None:
                raise RuntimeError("Bitrix generation resume history was not returned")
            return _non_negative_int(record, "max_resume_generation")

        return self._client.execute_read(_read)

    def list_child_runs(self, generation_id: str) -> tuple[GenerationChildRun, ...]:
        def _read(tx: ManagedTransaction) -> tuple[GenerationChildRun, ...]:
            result: list[GenerationChildRun] = []
            for record in tx.run(
                LIST_BITRIX_GENERATION_LOGICAL_RUNS,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
            ):
                stream = record["stream_key"]
                if stream is None:
                    continue
                if stream not in {"crm_deals", "crm_activities", "openlines_conversations"}:
                    raise RuntimeError("generation contains an invalid stream key")
                result.append(
                    GenerationChildRun(
                        stream_key=stream,
                        logical_run_id=_required_str(record["logical_run_id"], "logical_run_id"),
                        logical_status=_required_str(record["logical_status"], "logical_status"),
                        attempt_generation=_non_negative_int(record, "attempt_generation"),
                        stream_status=_optional_str(record["stream_status"]),
                        control_instance_id=self._control_instance_id,
                    )
                )
            return tuple(result)

        return self._client.execute_read(_read)

    def transition(
        self,
        generation_id: str,
        *,
        expected_statuses: tuple[str, ...],
        next_status: str,
        evidence_digest: str,
        actor: str,
    ) -> None:
        state = self.get_generation(generation_id)

        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                CAS_BITRIX_BACKFILL_GENERATION_STATUS,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                expected_statuses=list(expected_statuses),
                next_status=next_status,
                evidence_digest=evidence_digest,
                actor=actor,
                repository_sha=state.repository_sha,
                image_digest=state.image_digest,
                configuration_digest=state.configuration_digest,
                boundary_digest=state.boundary_digest,
            ).single()
            if record is None:
                raise RuntimeError("Bitrix generation CAS transition was rejected")

        self._client.execute_write(_work)

    def freeze(self, generation_id: str, *, reconciliation_digest: str) -> None:
        state = self.get_generation(generation_id)

        def _work(tx: ManagedTransaction) -> None:
            rows: list[FrozenOwnerRow] = []
            seen: set[str] = set()
            for record in tx.run(
                FREEZE_BITRIX_BACKFILL_GENERATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                repository_sha=state.repository_sha,
                image_digest=state.image_digest,
                configuration_digest=state.configuration_digest,
                boundary_digest=state.boundary_digest,
                reconciliation_digest=reconciliation_digest,
            ):
                deal_id = _required_str(record["deal_id"], "deal_id")
                if deal_id in seen:
                    raise RuntimeError("owner coverage contains duplicate deal IDs")
                seen.add(deal_id)
                rows.append(
                    FrozenOwnerRow(
                        deal_id=deal_id,
                        category_id=_required_str(record["category_id"], "category_id"),
                        stage_id=_optional_str(record["stage_id"]),
                        source_observation_hash=_required_str(
                            record["source_observation_hash"], "source_observation_hash"
                        ),
                    )
                )
            if not rows:
                raise RuntimeError("corrective generation has no in-scope owner coverage")
            owner_digest = _owner_set_digest(rows)
            completed = tx.run(
                COMPLETE_BITRIX_BACKFILL_FREEZE,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                owner_count=len(rows),
                owner_set_digest=owner_digest,
                reconciliation_digest=reconciliation_digest,
            ).single()
            if completed is None:
                raise RuntimeError("corrective generation could not freeze all child fences")

        self._client.execute_write(_work)

    def record_reconciliation(
        self,
        generation_id: str,
        *,
        stream_keys: tuple[BitrixStreamKey, ...],
        reconciliation_digest: str,
        actor: str,
    ) -> None:
        state = self.get_generation(generation_id)

        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                RECORD_BITRIX_BACKFILL_RECONCILIATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                stream_keys=list(stream_keys),
                reconciliation_digest=reconciliation_digest,
                actor=actor,
                repository_sha=state.repository_sha,
                image_digest=state.image_digest,
                configuration_digest=state.configuration_digest,
                boundary_digest=state.boundary_digest,
            ).single()
            if record is None:
                raise RuntimeError("atomic coverage reconciliation was rejected")

        self._client.execute_write(_work)

    def qualify(self, generation_id: str, result: QualificationResult) -> None:
        state = self.get_generation(generation_id)

        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                RECORD_BITRIX_QUALIFICATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                repository_sha=state.repository_sha,
                image_digest=state.image_digest,
                configuration_digest=state.configuration_digest,
                boundary_digest=state.boundary_digest,
                owner_artifact_id=result.owner_artifact_id,
                stage_artifact_id=result.stage_artifact_id,
                owner_recommendation=result.owner_recommendation,
                stage_recommendation=result.stage_recommendation,
                qualification_evidence_digest=result.evidence_digest,
            ).single()
            if record is None:
                raise RuntimeError("corrective generation rejected qualification evidence")

        self._client.execute_write(_work)

    def reject(
        self,
        generation_id: str,
        *,
        actor: str,
        reason: str,
        remediation: str,
    ) -> None:
        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                REJECT_BITRIX_BACKFILL_GENERATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                actor=actor,
                reason=reason,
                remediation=remediation,
            ).single()
            if record is None:
                raise RuntimeError("generation cannot be rejected from its current state")

        self._client.execute_write(_work)

    def get_generation_category_mapping(self, generation_id: str) -> dict[str, str]:
        if not generation_id.strip():
            raise ValueError("generation_id must be non-empty")

        def _read(tx: ManagedTransaction) -> dict[str, str]:
            records = tx.run(
                GET_BITRIX_GENERATION_CATEGORY_MAPPING,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
            )
            mapping: dict[str, str] = {}
            for record in records:
                category_id = _required_str(record["category_id"], "category_id")
                entity_keys = record["entity_keys"]
                if (
                    not isinstance(entity_keys, list)
                    or len(entity_keys) != 1
                    or not isinstance(entity_keys[0], str)
                    or not entity_keys[0]
                ):
                    raise RuntimeError("corrective category has ambiguous entity mapping evidence")
                mapping[category_id] = entity_keys[0]
            if not mapping:
                raise RuntimeError(
                    "accepted corrective generation has no category mapping evidence"
                )
            return mapping

        return self._client.execute_read(_read)

    def allocate_successor(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
        successor_boundary_digest: str,
    ) -> bool:
        creation_token = uuid.uuid4().hex

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                ALLOCATE_BITRIX_SUCCESSOR_GENERATION,
                control_instance_id=self._control_instance_id,
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                successor_boundary_digest=successor_boundary_digest,
                creation_token=creation_token,
            ).single()
            if record is None:
                raise RuntimeError("accepted corrective generation could not allocate successor")
            return record["created"] is True

        return self._client.execute_write(_work)

    def activate_successor(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
        actor: str,
        evidence_digest: str,
        occurrence: str,
    ) -> None:
        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                ACTIVATE_BITRIX_SUCCESSOR_GENERATION,
                control_instance_id=self._control_instance_id,
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                actor=actor,
                evidence_digest=evidence_digest,
                occurrence=occurrence,
            ).single()
            if record is None:
                raise RuntimeError("successor generation activation was rejected")

        self._client.execute_write(_work)

    def confirm_successor_publication(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
        actor: str,
        evidence_digest: str,
        canvas_id: str,
    ) -> None:
        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                CONFIRM_BITRIX_SUCCESSOR_PUBLICATION,
                control_instance_id=self._control_instance_id,
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                actor=actor,
                evidence_digest=evidence_digest,
                canvas_id=canvas_id,
            ).single()
            if record is None:
                raise RuntimeError("successor publication could not be confirmed safely")

        self._client.execute_write(_work)

    def get_confirmed_successor_canvas(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
        evidence_digest: str,
        occurrence: str,
    ) -> str:
        def _read(tx: ManagedTransaction) -> str:
            record = tx.run(
                GET_CONFIRMED_BITRIX_SUCCESSOR_PUBLICATION,
                control_instance_id=self._control_instance_id,
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                evidence_digest=evidence_digest,
                occurrence=occurrence,
            ).single()
            if record is None:
                raise RuntimeError("successor publication retry does not match stored evidence")
            return _required_str(record["canvas_id"], "successor_canvas_id")

        return self._client.execute_read(_read)

    def get_successor_publication_occurrence(self, successor_generation_id: str) -> str:
        def _read(tx: ManagedTransaction) -> str:
            record = tx.run(
                GET_BITRIX_SUCCESSOR_PUBLICATION_OCCURRENCE,
                control_instance_id=self._control_instance_id,
                successor_generation_id=successor_generation_id,
            ).single()
            if record is None:
                raise RuntimeError("active successor has no confirmed publication occurrence")
            return _required_str(record["occurrence"], "successor_publication_occurrence")

        return self._client.execute_read(_read)

    def supersede_zero_write_successor(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
        replacement_successor_generation_id: str,
        actor: str,
        reason: str,
        evidence_digest: str,
    ) -> None:
        def _work(tx: ManagedTransaction) -> None:
            record = tx.run(
                SUPERSEDE_ZERO_WRITE_BITRIX_SUCCESSOR,
                control_instance_id=self._control_instance_id,
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                replacement_successor_generation_id=replacement_successor_generation_id,
                actor=actor,
                reason=reason,
                evidence_digest=evidence_digest,
            ).single()
            if record is None:
                raise RuntimeError("failed successor could not be safely superseded")

        self._client.execute_write(_work)

    def verify_tail(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
    ) -> TailVerification:
        def _read(tx: ManagedTransaction) -> TailVerification:
            record = tx.run(
                VERIFY_BITRIX_SUCCESSOR_TAIL,
                control_instance_id=self._control_instance_id,
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
            ).single()
            if record is None:
                raise RuntimeError("successor cadence evidence is missing")
            return TailVerification(
                corrective_status=_required_str(record["corrective_status"], "corrective_status"),
                successor_status=_required_str(record["successor_status"], "successor_status"),
                predecessor_frozen=record["predecessor_frozen"] is True,
                historical_streams=_stream_key_tuple(record["historical_streams"]),
                expected_streams=_stream_key_tuple(record["expected_streams"]),
                historical_actual_streams=_stream_key_tuple(record["historical_actual_streams"]),
                actual_streams=_stream_key_tuple(record["actual_streams"]),
                historical_cadence_run_count=_non_negative_int(
                    record, "historical_cadence_run_count"
                ),
                historical_cadence_complete=record["historical_cadence_complete"] is True,
                cadence_run_count=_non_negative_int(record, "cadence_run_count"),
                cadence_complete=record["cadence_complete"] is True,
                historical_successor_coverage_count=_non_negative_int(
                    record, "historical_successor_coverage_count"
                ),
                historical_coverage_complete=record["historical_coverage_complete"] is True,
                successor_coverage_count=_non_negative_int(record, "successor_coverage_count"),
                coverage_complete=record["coverage_complete"] is True,
            )

        return self._client.execute_read(_read)

    def attach_logical_run(
        self,
        *,
        generation_id: str,
        stream_key: BitrixStreamKey,
        logical_run_id: str,
        fence_context: FenceContext,
        boundary_digest: str,
        configuration_digest: str,
    ) -> None:
        self._require_fence_control(fence_context)

        def _work(tx: ManagedTransaction) -> None:
            assert_active_bitrix_fence(tx, fence_context)
            record = tx.run(
                ATTACH_BACKFILL_LOGICAL_RUN,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                stream_key=stream_key,
                logical_run_id=logical_run_id,
                boundary_digest=boundary_digest,
                configuration_digest=configuration_digest,
            ).single()
            if record is None:
                raise RuntimeError("corrective generation rejected its child logical run")

        self._client.execute_write(_work)

    def materialize_known_owner_set(
        self,
        *,
        generation_id: str,
        membership_set_id: str,
        fence_context: FenceContext,
    ) -> KnownOwnerMembershipSet:
        if not generation_id.strip() or not membership_set_id.strip():
            raise ValueError("generation and membership set IDs must be non-empty")
        self._require_fence_control(fence_context)

        def _read(tx: ManagedTransaction) -> tuple[str, ...]:
            deal_ids: list[str] = []
            for record in tx.run(LIST_KNOWN_OWNER_IDS):
                deal_ids.append(_required_str(record["deal_id"], "deal_id"))
            return tuple(deal_ids)

        deal_ids = self._client.execute_read(_read)
        digest = _known_owner_digest(deal_ids)

        def _prepare(tx: ManagedTransaction) -> str:
            assert_active_bitrix_fence(tx, fence_context)
            record = tx.run(
                PREPARE_KNOWN_OWNER_SET,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                membership_set_id=membership_set_id,
                digest=digest,
                member_count=len(deal_ids),
            ).single()
            if record is None:
                raise RuntimeError("known-owner set conflicts with an existing snapshot")
            return _required_str(record["status"], "known_owner_status")

        status = self._client.execute_write(_prepare)
        if status == "building":
            for offset in range(0, len(deal_ids), _KNOWN_OWNER_BATCH_SIZE):
                batch: list[dict[str, str | int]] = [
                    {"deal_id": deal_id, "ordinal": ordinal}
                    for ordinal, deal_id in enumerate(
                        deal_ids[offset : offset + _KNOWN_OWNER_BATCH_SIZE],
                        start=offset,
                    )
                ]

                def _write_batch(
                    tx: ManagedTransaction,
                    _batch: list[dict[str, str | int]] = batch,
                ) -> None:
                    assert_active_bitrix_fence(tx, fence_context)
                    record = tx.run(
                        UPSERT_KNOWN_OWNER_MEMBERS,
                        control_instance_id=self._control_instance_id,
                        generation_id=generation_id,
                        membership_set_id=membership_set_id,
                        digest=digest,
                        members=_batch,
                    ).single()
                    if record is None or _non_negative_int(record, "batch_count") != len(_batch):
                        raise RuntimeError("known-owner membership batch did not reconcile")

                self._client.execute_write(_write_batch)

            def _seal(tx: ManagedTransaction) -> None:
                assert_active_bitrix_fence(tx, fence_context)
                record = tx.run(
                    SEAL_KNOWN_OWNER_SET,
                    control_instance_id=self._control_instance_id,
                    generation_id=generation_id,
                    membership_set_id=membership_set_id,
                    digest=digest,
                ).single()
                if record is None:
                    raise RuntimeError("known-owner set changed before it could be sealed")
                if _non_negative_int(record, "member_count") != len(deal_ids):
                    raise RuntimeError("known-owner membership count did not reconcile")
                if _required_str(record["digest"], "known_owner_digest") != digest:
                    raise RuntimeError("known-owner membership digest did not reconcile")

            self._client.execute_write(_seal)
        elif status != "sealed":
            raise RuntimeError("known-owner set has an invalid build status")
        return KnownOwnerMembershipSet(
            generation_id=generation_id,
            membership_set_id=membership_set_id,
            digest=digest,
            deal_ids=deal_ids,
            control_instance_id=self._control_instance_id,
        )

    def find_known_owner_set(
        self,
        *,
        generation_id: str,
        membership_set_id: str,
    ) -> KnownOwnerMembershipSet | None:
        def _read_metadata(tx: ManagedTransaction) -> tuple[str, int, str] | None:
            record = tx.run(
                GET_KNOWN_OWNER_SET,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                membership_set_id=membership_set_id,
            ).single()
            if record is None:
                return None
            return (
                _required_str(record["digest"], "known_owner_digest"),
                _non_negative_int(record, "member_count"),
                _required_str(record["status"], "known_owner_status"),
            )

        metadata = self._client.execute_read(_read_metadata)
        if metadata is None:
            return None
        digest, expected_count, status = metadata
        if status == "building":
            return None
        if status != "sealed":
            raise RuntimeError("known-owner set has an invalid build status")
        deal_ids: list[str] = []
        after_ordinal = -1
        while len(deal_ids) < expected_count:

            def _read_page(
                tx: ManagedTransaction,
                _after_ordinal: int = after_ordinal,
            ) -> tuple[tuple[int, str], ...]:
                rows: list[tuple[int, str]] = []
                for record in tx.run(
                    LIST_KNOWN_OWNER_MEMBERS_PAGE,
                    control_instance_id=self._control_instance_id,
                    generation_id=generation_id,
                    membership_set_id=membership_set_id,
                    after_ordinal=_after_ordinal,
                    limit=_KNOWN_OWNER_BATCH_SIZE,
                ):
                    rows.append(
                        (
                            _non_negative_int(record, "ordinal"),
                            _required_str(record["deal_id"], "deal_id"),
                        )
                    )
                return tuple(rows)

            page = self._client.execute_read(_read_page)
            if not page:
                raise RuntimeError("known-owner membership ended before its declared count")
            for ordinal, deal_id in page:
                if ordinal != len(deal_ids):
                    raise RuntimeError("known-owner membership ordinals are not contiguous")
                deal_ids.append(deal_id)
            after_ordinal = page[-1][0]
        if len(deal_ids) != expected_count:
            raise RuntimeError("known-owner membership count did not reconcile")
        frozen_ids = tuple(deal_ids)
        if _known_owner_digest(frozen_ids) != digest:
            raise RuntimeError("known-owner membership digest did not reconcile")
        return KnownOwnerMembershipSet(
            generation_id=generation_id,
            membership_set_id=membership_set_id,
            digest=digest,
            deal_ids=frozen_ids,
            control_instance_id=self._control_instance_id,
        )

    def get_known_owner_set(
        self,
        *,
        generation_id: str,
        membership_set_id: str,
    ) -> KnownOwnerMembershipSet:
        membership = self.find_known_owner_set(
            generation_id=generation_id,
            membership_set_id=membership_set_id,
        )
        if membership is None:
            raise RuntimeError("corrective generation has no sealed known-owner set")
        return membership

    def reconcile_coverage(
        self,
        *,
        generation_id: str,
        stream_key: BitrixStreamKey,
    ) -> CoverageReconciliation:
        def _read(tx: ManagedTransaction) -> CoverageReconciliation:
            record = tx.run(
                GET_BITRIX_COVERAGE_RECONCILIATION,
                control_instance_id=self._control_instance_id,
                generation_id=generation_id,
                stream_key=stream_key,
            ).single()
            if record is None:
                raise RuntimeError("corrective stream has no coverage reconciliation row")
            return CoverageReconciliation(
                stream_key=stream_key,
                coverage_count=_non_negative_int(record, "coverage_count"),
                terminal_count=_non_negative_int(record, "terminal_count"),
                created_count=_non_negative_int(record, "created_count"),
                duplicate_count=_non_negative_int(record, "duplicate_count"),
                projection_count=_non_negative_int(record, "projection_count"),
                unchanged_count=_non_negative_int(record, "unchanged_count"),
                excluded_count=_non_negative_int(record, "excluded_count"),
                quarantine_count=_non_negative_int(record, "quarantine_count"),
                conflict_count=_non_negative_int(record, "conflict_count"),
                failed_count=_non_negative_int(record, "failed_count"),
                checkpoint_committed_count=_non_negative_int(record, "checkpoint_committed_count"),
                checkpoint_duplicate_count=_non_negative_int(record, "checkpoint_duplicate_count"),
                checkpoint_excluded_count=_non_negative_int(record, "checkpoint_excluded_count"),
                checkpoint_retry_count=_non_negative_int(record, "checkpoint_retry_count"),
            )

        return self._client.execute_read(_read)

    @staticmethod
    def record_coverage_in_transaction(
        tx: ManagedTransaction,
        *,
        generation_id: str,
        stream_key: BitrixStreamKey,
        fence_context: FenceContext,
        entry: CoverageEntry,
    ) -> None:
        assert_active_bitrix_fence(tx, fence_context)
        record = tx.run(
            UPSERT_BITRIX_BACKFILL_COVERAGE,
            control_instance_id=fence_context.control_instance_id,
            generation_id=generation_id,
            stream_key=stream_key,
            logical_run_id=fence_context.logical_run_id,
            ingest_run_id=fence_context.ingest_run_id,
            attempt_generation=fence_context.attempt_generation,
            stream_generation=fence_context.stream_generation,
            fencing_token=fence_context.fencing_token,
            source_identity=entry.source_identity,
            source_boundary=entry.source_boundary,
            disposition=entry.disposition,
            source_observation_hash=entry.source_observation_hash,
            terminal=entry.terminal,
            deal_id=entry.deal_id,
            scope_state=entry.scope_state,
            entity_key=entry.entity_key,
            category_id=entry.category_id,
            stage_id=entry.stage_id,
            census_epoch=entry.census_epoch,
            detail=entry.detail,
            outcome_digest=entry.outcome_digest,
            creation_token=uuid.uuid4().hex,
        ).single()
        if record is None:
            raise RuntimeError("coverage identity conflicts with an existing terminal outcome")

    def export_frozen_owners(self, generation_id: str) -> FrozenOwnerExport:
        if not generation_id.strip():
            raise ValueError("generation_id must be non-empty")

        def _work(tx: ManagedTransaction) -> FrozenOwnerExport:
            records: list[Record] = list(
                tx.run(
                    EXPORT_FROZEN_OWNER_COVERAGE,
                    control_instance_id=self._control_instance_id,
                    generation_id=generation_id,
                )
            )
            if not records:
                raise RuntimeError("frozen corrective generation has no in-scope owner coverage")
            boundary = records[0]["boundary_digest"]
            if not isinstance(boundary, str) or not boundary:
                raise RuntimeError("frozen corrective generation omitted its boundary digest")
            rows: list[FrozenOwnerRow] = []
            seen: set[str] = set()
            for record in records:
                if record["boundary_digest"] != boundary:
                    raise RuntimeError("frozen owner coverage has inconsistent boundaries")
                deal_id = _required_str(record["deal_id"], "deal_id")
                if deal_id in seen:
                    raise RuntimeError("frozen owner coverage contains duplicate deal IDs")
                seen.add(deal_id)
                stage = record["stage_id"]
                rows.append(
                    FrozenOwnerRow(
                        deal_id=deal_id,
                        category_id=_required_str(record["category_id"], "category_id"),
                        stage_id=stage if isinstance(stage, str) and stage else None,
                        source_observation_hash=_required_str(
                            record["source_observation_hash"],
                            "source_observation_hash",
                        ),
                    )
                )
            source_contract_uuid = _consistent_string(records, "source_contract_uuid")
            configuration_digest = _consistent_string(records, "configuration_digest")
            image_digest = _consistent_string(records, "image_digest")
            expected_count = records[0]["expected_owner_count"]
            if (
                isinstance(expected_count, bool)
                or not isinstance(expected_count, int)
                or expected_count != len(rows)
            ):
                raise RuntimeError("frozen owner coverage count does not reconcile")
            expected_digest = _consistent_string(records, "expected_owner_set_digest")
            actual_digest = _owner_set_digest(rows)
            if actual_digest != expected_digest:
                raise RuntimeError("frozen owner coverage digest does not reconcile")
            return FrozenOwnerExport(
                generation_id=generation_id,
                source_contract_uuid=source_contract_uuid,
                configuration_digest=configuration_digest,
                image_digest=image_digest,
                boundary_digest=boundary,
                owner_set_digest=actual_digest,
                rows=tuple(rows),
                control_instance_id=self._control_instance_id,
            )

        return self._client.execute_read(_work)


def _required_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"frozen owner coverage contains an invalid {label}")
    return value


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _stream_key_tuple(value: object) -> tuple[BitrixStreamKey, ...]:
    if not isinstance(value, list):
        raise RuntimeError("tail verification stream inventory is invalid")
    result: list[BitrixStreamKey] = []
    for item in value:
        if item not in {"crm_deals", "crm_activities", "openlines_conversations"}:
            raise RuntimeError("tail verification contains an invalid stream key")
        result.append(item)
    return tuple(result)


def _consistent_string(records: list[Record], key: str) -> str:
    values = {record[key] for record in records}
    if len(values) != 1:
        raise RuntimeError(f"frozen owner coverage has inconsistent {key}")
    return _required_str(values.pop(), key)


def _owner_set_digest(rows: list[FrozenOwnerRow]) -> str:
    payload = [
        {
            "deal_id": row.deal_id,
            "category_id": row.category_id,
            "stage_id": row.stage_id,
            "source_observation_hash": row.source_observation_hash,
        }
        for row in rows
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"bitrix-frozen-owner-set-v1\x00" + encoded).hexdigest()


def _known_owner_digest(deal_ids: tuple[str, ...]) -> str:
    encoded = json.dumps(deal_ids, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"bitrix-known-owner-set-v1\x00" + encoded).hexdigest()


def _non_negative_int(record: Record, key: str) -> int:
    value: object = record[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"coverage reconciliation contains an invalid {key}")
    return value
