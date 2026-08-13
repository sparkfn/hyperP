"""Operator CLI for the fenced Bitrix corrective backfill and successor cutover."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import cast

from pydantic import TypeAdapter

from src.bitrix_backfill_models import (
    BackfillInventoryEntry,
    BackfillInventoryManifest,
    InventoryReplayMode,
    QualificationResult,
    RollbackStatus,
)
from src.bitrix_ingestion_models import BitrixStreamKey
from src.config import get_settings
from src.connectors.bitrix_stage_history.artifact_runtime import (
    ArtifactStoreConfiguration,
    decode_signing_secret,
    retained_keys_from_environment,
)
from src.connectors.bitrix_stage_history.artifact_store import ArtifactStore
from src.connectors.bitrix_stage_history.replay import qualify_artifacts
from src.graph.bitrix_backfill import BitrixBackfillRepository
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import LogicalRunControl
from src.ingestion_config import (
    BitrixOpenLinesConfig,
    bitrix_configuration_digest,
    bitrix_legacy_explicit_category_digest,
    get_ingestion_config,
)
from src.models import JsonValue

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_TERMINAL_CHILD_STATUSES = frozenset({"completed", "completed_with_errors"})
CONTROL_COMMANDS = frozenset(
    {
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
)


def load_inventory(path: Path) -> BackfillInventoryManifest:
    payload = _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))
    raw_entries = _required_list(payload, "entries")
    entries: list[BackfillInventoryEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("inventory entries must be JSON objects")
        stream = _required_text(raw_entry, "stream_key")
        if stream not in {"crm_deals", "crm_activities", "openlines_conversations"}:
            raise ValueError("inventory contains an unsupported Bitrix stream")
        replay_mode = _required_text(raw_entry, "replay_mode")
        if replay_mode not in {
            "strict_keyset",
            "fixed_keyset",
            "targeted_refresh",
            "bounded_replay",
            "excluded",
        }:
            raise ValueError("inventory contains an unsupported replay mode")
        raw_window = raw_entry.get("source_window")
        if raw_window is not None and not isinstance(raw_window, dict):
            raise ValueError("inventory source_window must be an object or null")
        entries.append(
            BackfillInventoryEntry(
                gap_id=_required_text(raw_entry, "gap_id"),
                stream_key=cast(BitrixStreamKey, stream),
                bounded_population=_required_int(raw_entry, "bounded_population"),
                current_count=_required_int(raw_entry, "current_count"),
                source_basis=_required_text(raw_entry, "source_basis"),
                expected_repair=_required_text(raw_entry, "expected_repair"),
                replay_mode=cast(InventoryReplayMode, replay_mode),
                source_window=dict(raw_window) if raw_window is not None else None,
                completion_equation=_required_text(raw_entry, "completion_equation"),
                max_calls=_required_int(raw_entry, "max_calls"),
                max_rows=_required_int(raw_entry, "max_rows"),
                max_runtime_seconds=_required_int(raw_entry, "max_runtime_seconds"),
                max_storage_bytes=_required_int(raw_entry, "max_storage_bytes"),
                max_lock_seconds=_required_int(raw_entry, "max_lock_seconds"),
                max_lag_seconds=_required_int(raw_entry, "max_lag_seconds"),
                rollback_path=_required_text(raw_entry, "rollback_path"),
                reviewed_exclusion=_optional_text(raw_entry.get("reviewed_exclusion")),
            )
        )
    return BackfillInventoryManifest(
        source_key=_required_text(payload, "source_key"),
        reviewed_by=_required_text(payload, "reviewed_by"),
        backup_id=_required_text(payload, "backup_id"),
        backup_restore_evidence_digest=_required_text(payload, "backup_restore_evidence_digest"),
        minimum_fence_image_digest=_required_text(payload, "minimum_fence_image_digest"),
        legacy_dispatch_paused=_required_bool(payload, "legacy_dispatch_paused"),
        predecessor_quiescent=_required_bool(payload, "predecessor_quiescent"),
        entries=tuple(entries),
    )


def load_qualification(path: Path) -> QualificationResult:
    payload = _JSON_OBJECT.validate_json(path.read_text(encoding="utf-8"))
    return _qualification_from_payload(payload)


def _qualification_from_payload(payload: dict[str, JsonValue]) -> QualificationResult:
    if _required_bool(payload, "deterministic_replay") is not True:
        raise ValueError("qualification replay was not deterministic")
    if _required_int(payload, "source_calls") != 0:
        raise ValueError("qualification evidence contains source calls")
    if _required_int(payload, "graph_writes") != 0:
        raise ValueError("qualification evidence contains graph writes")
    derived = payload.get("derived")
    if not isinstance(derived, dict):
        raise ValueError("qualification evidence omitted derived replay results")
    replay_digest = _digest_text(
        "sealed-artifact-replay",
        json.dumps(derived, sort_keys=True, separators=(",", ":")),
    )
    return QualificationResult(
        owner_artifact_id=_required_text(payload, "owner_artifact_id"),
        stage_artifact_id=_required_text(payload, "stage_artifact_id"),
        owner_recommendation=_required_text(payload, "owner_recommendation"),
        stage_recommendation=_required_text(payload, "stage_recommendation"),
        replay_digest=replay_digest,
        stage_domain_writes=_required_int(payload, "stage_domain_writes"),
    )


class BitrixBackfillControl:
    def __init__(self, client: Neo4jClient) -> None:
        self._client = client
        self._repository = BitrixBackfillRepository(client)

    def allocate(
        self,
        generation_id: str,
        manifest: BackfillInventoryManifest,
        *,
        repository_sha: str,
        image_digest: str,
        configuration_digest: str,
        source_contract_uuid: str,
        boundary_digest: str,
    ) -> bool:
        from src.bitrix_backfill_models import GenerationProvenance

        if image_digest != manifest.minimum_fence_image_digest:
            raise ValueError("allocation image is below or different from the reviewed fence floor")
        _require_new_generation_configuration(configuration_digest)
        created = self._repository.allocate_generation(
            generation_id,
            GenerationProvenance(
                repository_sha=repository_sha,
                image_digest=image_digest,
                configuration_digest=configuration_digest,
                source_contract_uuid=source_contract_uuid,
                boundary_digest=boundary_digest,
            ),
        )
        self._repository.register_inventory(generation_id, manifest)
        return created

    def start(self, generation_id: str, *, actor: str) -> str:
        from src.bitrix_backfill_tasks import dispatch_generation_canvas

        state = self._repository.get_generation(generation_id)
        manifest = self._manifest_for(generation_id)
        if state.status == "allocated":
            self._repository.transition(
                generation_id,
                expected_statuses=("allocated",),
                next_status="backfilling",
                evidence_digest=manifest.digest,
                actor=actor,
            )
        elif state.status != "backfilling":
            raise RuntimeError("start requires an allocated or backfilling corrective generation")
        return dispatch_generation_canvas(
            generation_id=generation_id,
            boundary_digest=state.boundary_digest,
            configuration_digest=state.configuration_digest,
            entries=manifest.executable_entries,
        )

    def request_stop(self, generation_id: str, *, actor: str, reason: str) -> int:
        runs = self._repository.list_child_runs(generation_id)
        control = LogicalRunControl(self._client)
        requested = 0
        for run in runs:
            if run.logical_status in _TERMINAL_CHILD_STATUSES:
                continue
            if (
                control.request_stop(
                    logical_run_id=run.logical_run_id,
                    requested_by=actor,
                    reason=reason,
                )
                is not None
            ):
                requested += 1
        return requested

    def resume(self, generation_id: str, *, occurrence: str | None = None) -> str:
        from src.bitrix_backfill_tasks import dispatch_generation_canvas

        state = self._repository.get_generation(generation_id)
        corrective_resume = state.generation_kind == "corrective" and state.status == "backfilling"
        successor_resume = state.generation_kind == "live_successor" and state.status == "active"
        if not corrective_resume and not successor_resume:
            raise RuntimeError("resume requires a backfilling corrective or active successor")
        if successor_resume:
            stored_occurrence = self._repository.get_successor_publication_occurrence(generation_id)
            if occurrence is not None and occurrence != stored_occurrence:
                raise ValueError("successor resume occurrence does not match activation evidence")
            occurrence = stored_occurrence
        runs = self._repository.list_child_runs(generation_id)
        resumable = [
            run for run in runs if run.logical_status in {"paused_with_checkpoint", "failed"}
        ]
        if not resumable:
            raise RuntimeError("generation has no paused or failed child run")
        manifest = self._manifest_for(generation_id)
        inventory_streams = {entry.stream_key for entry in manifest.executable_entries}
        if any(run.stream_key not in inventory_streams for run in resumable):
            raise RuntimeError("resumable child runs do not match the generation inventory")
        resume_generation = (
            max(
                max(run.attempt_generation for run in runs),
                self._repository.get_max_resume_worker_generation(generation_id),
            )
            + 1
        )
        return dispatch_generation_canvas(
            generation_id=generation_id,
            boundary_digest=state.boundary_digest,
            configuration_digest=state.configuration_digest,
            entries=manifest.executable_entries,
            resume_generation=resume_generation,
            task_kind="live" if successor_resume else "corrective",
            occurrence=occurrence,
        )

    def reconcile(self, generation_id: str, *, actor: str) -> str:
        state = self._repository.get_generation(generation_id)
        if state.status not in {"backfilling", "reconciling"}:
            raise RuntimeError("reconcile requires a backfilling or reconciling generation")
        manifest = self._manifest_for(generation_id)
        expected_streams = {entry.stream_key for entry in manifest.executable_entries}
        runs = self._repository.list_child_runs(generation_id)
        completed = {
            run.stream_key for run in runs if run.logical_status in _TERMINAL_CHILD_STATUSES
        }
        if completed != expected_streams:
            raise RuntimeError("corrective child runs are incomplete or do not match inventory")
        evidence: list[dict[str, JsonValue]] = []
        for entry in manifest.executable_entries:
            reconciliation = self._repository.reconcile_coverage(
                generation_id=generation_id,
                stream_key=entry.stream_key,
            )
            if not reconciliation.complete:
                raise RuntimeError(f"coverage reconciliation failed for {entry.stream_key}")
            if reconciliation.coverage_count != entry.bounded_population:
                raise RuntimeError(
                    f"{entry.stream_key} coverage does not equal reviewed bounded population"
                )
            if reconciliation.coverage_count > entry.max_rows:
                raise RuntimeError(f"{entry.stream_key} exceeded its approved row ceiling")
            evidence.append(cast(dict[str, JsonValue], asdict(reconciliation)))
        encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        digest = _digest_text("coverage-reconciliation", encoded)
        self._repository.record_reconciliation(
            generation_id,
            stream_keys=tuple(entry.stream_key for entry in manifest.executable_entries),
            reconciliation_digest=digest,
            actor=actor,
        )
        return digest

    def freeze(self, generation_id: str, *, reconciliation_digest: str) -> None:
        self._repository.freeze(
            generation_id,
            reconciliation_digest=reconciliation_digest,
        )

    def qualify_from_artifacts(
        self,
        generation_id: str,
        store: ArtifactStore,
        *,
        owner_artifact_id: str,
        stage_artifact_id: str,
    ) -> QualificationResult:
        state = self._repository.get_generation(generation_id)
        if state.status != "frozen":
            raise RuntimeError("qualification requires a frozen corrective generation")
        owner = store.verify(owner_artifact_id)
        stage = store.verify(stage_artifact_id)
        provenance = owner.provenance
        expected = (
            state.source_contract_uuid,
            state.repository_sha,
            state.image_digest,
            state.configuration_digest,
        )
        actual = (
            provenance.source_contract_uuid,
            provenance.repository_sha,
            provenance.image_digest,
            provenance.configuration_digest,
        )
        if actual != expected:
            raise RuntimeError("owner artifact provenance does not match the frozen generation")
        stage_provenance = stage.provenance
        stage_identity = (
            stage_provenance.source_contract_uuid,
            stage_provenance.repository_sha,
            stage_provenance.image_digest,
            stage_provenance.configuration_digest,
        )
        if stage_identity != actual:
            raise RuntimeError("stage artifact provenance does not match the owner artifact")
        boundaries = _JSON_OBJECT.validate_json(provenance.restricted_boundaries_json)
        if boundaries.get("generation_id") != generation_id:
            raise RuntimeError("owner artifact belongs to a different corrective generation")
        if boundaries.get("boundary_digest") != state.boundary_digest:
            raise RuntimeError("owner artifact boundary does not match the frozen generation")
        payload = qualify_artifacts(
            store,
            owner_artifact_id=owner_artifact_id,
            stage_artifact_id=stage_artifact_id,
        )
        result = _qualification_from_payload(payload)
        self._repository.qualify(generation_id, result)
        return result

    def accept(self, generation_id: str, *, actor: str, reason: str) -> None:
        state = self._repository.get_generation(generation_id)
        if state.status != "qualified":
            raise RuntimeError("acceptance requires a qualified corrective generation")
        _require_runtime_configuration(
            state.configuration_digest,
            generation_id=generation_id,
            repository=self._repository,
        )
        evidence = _digest_text("human-acceptance", f"{actor}\x00{reason}")
        self._repository.transition(
            generation_id,
            expected_statuses=("qualified",),
            next_status="accepted",
            evidence_digest=evidence,
            actor=actor,
        )

    def reject(
        self,
        generation_id: str,
        *,
        actor: str,
        reason: str,
        remediation: str,
    ) -> None:
        self._repository.reject(
            generation_id,
            actor=actor,
            reason=reason,
            remediation=remediation,
        )

    def activate(
        self,
        *,
        corrective_generation_id: str,
        successor_generation_id: str,
        manifest: BackfillInventoryManifest,
        successor_boundary_digest: str,
        occurrence: str,
        actor: str,
    ) -> str:
        from src.bitrix_backfill_tasks import dispatch_generation_canvas

        corrective = self._repository.get_generation(corrective_generation_id)
        if corrective.status != "accepted":
            raise RuntimeError("only an accepted corrective generation can activate a successor")
        _require_runtime_configuration(
            corrective.configuration_digest,
            generation_id=corrective_generation_id,
            repository=self._repository,
        )
        self._repository.allocate_successor(
            corrective_generation_id=corrective_generation_id,
            successor_generation_id=successor_generation_id,
            successor_boundary_digest=successor_boundary_digest,
        )
        successor = self._repository.get_generation(successor_generation_id)
        evidence = _digest_text(
            "successor-activation",
            f"{corrective_generation_id}\x00{manifest.digest}\x00{occurrence}",
        )
        if successor.status == "allocated":
            self._repository.register_inventory(successor_generation_id, manifest)
        elif successor.status in {"activating", "active"}:
            stored_manifest = self._manifest_for(successor_generation_id)
            if stored_manifest.digest != manifest.digest:
                raise RuntimeError("activation retry changed the successor inventory")
            if successor.status == "active":
                return self._repository.get_confirmed_successor_canvas(
                    corrective_generation_id=corrective_generation_id,
                    successor_generation_id=successor_generation_id,
                    evidence_digest=evidence,
                    occurrence=occurrence,
                )
        else:
            raise RuntimeError("successor is not in an activatable state")
        try:
            self._repository.activate_successor(
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                actor=actor,
                evidence_digest=evidence,
                occurrence=occurrence,
            )
        except RuntimeError:
            refreshed = self._repository.get_generation(successor_generation_id)
            if refreshed.status == "active":
                return self._repository.get_confirmed_successor_canvas(
                    corrective_generation_id=corrective_generation_id,
                    successor_generation_id=successor_generation_id,
                    evidence_digest=evidence,
                    occurrence=occurrence,
                )
            raise
        successor = self._repository.get_generation(successor_generation_id)
        canvas_id = dispatch_generation_canvas(
            generation_id=successor_generation_id,
            boundary_digest=successor.boundary_digest,
            configuration_digest=successor.configuration_digest,
            entries=manifest.executable_entries,
            task_kind="live",
            occurrence=occurrence,
        )
        try:
            self._repository.confirm_successor_publication(
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                actor=actor,
                evidence_digest=evidence,
                canvas_id=canvas_id,
            )
            return canvas_id
        except RuntimeError:
            return self._repository.get_confirmed_successor_canvas(
                corrective_generation_id=corrective_generation_id,
                successor_generation_id=successor_generation_id,
                evidence_digest=evidence,
                occurrence=occurrence,
            )

    def recover_successor(
        self,
        *,
        corrective_generation_id: str,
        failed_successor_generation_id: str,
        replacement_successor_generation_id: str,
        manifest: BackfillInventoryManifest,
        successor_boundary_digest: str,
        occurrence: str,
        actor: str,
        reason: str,
    ) -> str:
        """Fence a zero-write failed successor and publish a distinct replacement."""
        if failed_successor_generation_id == replacement_successor_generation_id:
            raise ValueError("replacement successor generation must be distinct")
        if not reason.strip():
            raise ValueError("successor recovery reason must be non-empty")
        corrective = self._repository.get_generation(corrective_generation_id)
        if corrective.status != "accepted":
            raise RuntimeError("successor recovery requires an accepted corrective generation")
        _require_runtime_configuration(
            corrective.configuration_digest,
            generation_id=corrective_generation_id,
            repository=self._repository,
        )
        failed = self._repository.get_generation(failed_successor_generation_id)
        if (
            failed.generation_kind != "live_successor"
            or failed.corrective_generation_id != corrective_generation_id
        ):
            raise RuntimeError("failed successor does not belong to the corrective generation")
        if failed.material_write_count != 0:
            raise RuntimeError("successor recovery is limited to zero-write failures")
        evidence = _digest_text(
            "zero-write-successor-recovery",
            f"{corrective_generation_id}\x00{failed_successor_generation_id}"
            f"\x00{replacement_successor_generation_id}\x00{actor}\x00{reason}",
        )
        self._repository.supersede_zero_write_successor(
            corrective_generation_id=corrective_generation_id,
            successor_generation_id=failed_successor_generation_id,
            replacement_successor_generation_id=replacement_successor_generation_id,
            actor=actor,
            reason=reason,
            evidence_digest=evidence,
        )
        return self.activate(
            corrective_generation_id=corrective_generation_id,
            successor_generation_id=replacement_successor_generation_id,
            manifest=manifest,
            successor_boundary_digest=successor_boundary_digest,
            occurrence=occurrence,
            actor=actor,
        )

    def rollback_status(
        self,
        generation_id: str,
        *,
        successor_generation_id: str | None = None,
    ) -> RollbackStatus:
        corrective = self._repository.get_generation(generation_id)
        if successor_generation_id is None and corrective.material_write_count == 0:
            return RollbackStatus(
                "pre_write_image_rollback",
                corrective.status == "rejected",
                "deploy any tested compatible image; no corrective data compensation is required",
            )
        successor = (
            self._repository.get_generation(successor_generation_id)
            if successor_generation_id is not None
            else None
        )
        if successor is not None and successor.material_write_count == 0:
            return RollbackStatus(
                "post_activation_pre_write_supersession",
                True,
                "disable dispatch, supersede the successor, and keep the fence-aware image floor",
            )
        return RollbackStatus(
            "post_write_compensation_or_restore",
            True,
            "keep Bitrix dispatch blocked; execute reconciled compensation or "
            "tested backup restore",
        )

    def _manifest_for(self, generation_id: str) -> BackfillInventoryManifest:
        manifest_json, expected_digest = self._repository.get_inventory_json(generation_id)
        payload = _JSON_OBJECT.validate_json(manifest_json)
        manifest = _manifest_from_payload(payload)
        if manifest.digest != expected_digest:
            raise RuntimeError("stored inventory digest does not verify")
        return manifest


def _manifest_from_payload(payload: dict[str, JsonValue]) -> BackfillInventoryManifest:
    raw_entries = _required_list(payload, "entries")
    entries: list[BackfillInventoryEntry] = []
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            raise ValueError("inventory entries must be objects")
        stream = _required_text(raw_entry, "stream_key")
        replay = _required_text(raw_entry, "replay_mode")
        window = raw_entry.get("source_window")
        entries.append(
            BackfillInventoryEntry(
                gap_id=_required_text(raw_entry, "gap_id"),
                stream_key=cast(BitrixStreamKey, stream),
                bounded_population=_required_int(raw_entry, "bounded_population"),
                current_count=_required_int(raw_entry, "current_count"),
                source_basis=_required_text(raw_entry, "source_basis"),
                expected_repair=_required_text(raw_entry, "expected_repair"),
                replay_mode=cast(InventoryReplayMode, replay),
                source_window=dict(window) if isinstance(window, dict) else None,
                completion_equation=_required_text(raw_entry, "completion_equation"),
                max_calls=_required_int(raw_entry, "max_calls"),
                max_rows=_required_int(raw_entry, "max_rows"),
                max_runtime_seconds=_required_int(raw_entry, "max_runtime_seconds"),
                max_storage_bytes=_required_int(raw_entry, "max_storage_bytes"),
                max_lock_seconds=_required_int(raw_entry, "max_lock_seconds"),
                max_lag_seconds=_required_int(raw_entry, "max_lag_seconds"),
                rollback_path=_required_text(raw_entry, "rollback_path"),
                reviewed_exclusion=_optional_text(raw_entry.get("reviewed_exclusion")),
            )
        )
    return BackfillInventoryManifest(
        source_key=_required_text(payload, "source_key"),
        reviewed_by=_required_text(payload, "reviewed_by"),
        backup_id=_required_text(payload, "backup_id"),
        backup_restore_evidence_digest=_required_text(payload, "backup_restore_evidence_digest"),
        minimum_fence_image_digest=_required_text(payload, "minimum_fence_image_digest"),
        legacy_dispatch_paused=_required_bool(payload, "legacy_dispatch_paused"),
        predecessor_quiescent=_required_bool(payload, "predecessor_quiescent"),
        entries=tuple(entries),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    inventory.add_argument("--manifest", type=Path, required=True)
    allocate = commands.add_parser("allocate")
    _generation_argument(allocate)
    allocate.add_argument("--manifest", type=Path, required=True)
    for name in (
        "repository-sha",
        "image-digest",
        "configuration-digest",
        "source-contract-uuid",
        "boundary-digest",
    ):
        allocate.add_argument(f"--{name}", required=True)
    generation_commands = (
        "start",
        "status",
        "resume",
        "reconcile",
        "freeze",
        "qualify",
        "accept",
        "reject",
        "request-stop",
        "rollback-status",
    )
    for name in generation_commands:
        command = commands.add_parser(name)
        _generation_argument(command)
        if name in {"start", "reconcile", "accept", "reject", "request-stop"}:
            command.add_argument("--actor", required=True)
        if name in {"accept", "reject", "request-stop"}:
            command.add_argument("--reason", required=True)
        if name == "reject":
            command.add_argument("--remediation", required=True)
        if name == "freeze":
            command.add_argument("--reconciliation-digest", required=True)
        if name == "qualify":
            command.add_argument("--owner-artifact-id", required=True)
            command.add_argument("--stage-artifact-id", required=True)
            command.add_argument("--artifact-primary-root", type=Path, required=True)
            command.add_argument("--artifact-backup-root", type=Path, required=True)
            command.add_argument("--artifact-signing-key-id", default="bitrix-artifact-v1")
            command.add_argument(
                "--artifact-signing-key-env",
                default="BITRIX_ARTIFACT_SIGNING_KEY",
            )
            command.add_argument("--artifact-retained-key-env", action="append", default=[])
        if name == "rollback-status":
            command.add_argument("--successor-generation-id")
        if name == "resume":
            command.add_argument("--occurrence")
    activate = commands.add_parser("activate")
    activate.add_argument("--generation-id", required=True)
    activate.add_argument("--successor-generation-id", required=True)
    activate.add_argument("--manifest", type=Path, required=True)
    activate.add_argument("--successor-boundary-digest", required=True)
    activate.add_argument("--occurrence", required=True)
    activate.add_argument("--actor", required=True)
    recover = commands.add_parser("recover-successor")
    recover.add_argument("--generation-id", required=True)
    recover.add_argument("--failed-successor-generation-id", required=True)
    recover.add_argument("--replacement-successor-generation-id", required=True)
    recover.add_argument("--manifest", type=Path, required=True)
    recover.add_argument("--successor-boundary-digest", required=True)
    recover.add_argument("--occurrence", required=True)
    recover.add_argument("--actor", required=True)
    recover.add_argument("--reason", required=True)
    verify = commands.add_parser("verify-tail")
    verify.add_argument("--generation-id", required=True)
    verify.add_argument("--successor-generation-id", required=True)
    return parser


def run(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.command == "inventory":
        manifest = load_inventory(args.manifest)
        _print({"inventory_digest": manifest.digest, "entries": len(manifest.entries)})
        return 0
    client = Neo4jClient(get_settings())
    try:
        control = BitrixBackfillControl(client)
        generation_id = cast(str, args.generation_id)
        if args.command == "allocate":
            manifest = load_inventory(args.manifest)
            created = control.allocate(
                generation_id,
                manifest,
                repository_sha=args.repository_sha,
                image_digest=args.image_digest,
                configuration_digest=args.configuration_digest,
                source_contract_uuid=args.source_contract_uuid,
                boundary_digest=args.boundary_digest,
            )
            _print({"generation_id": generation_id, "created": created, "status": "allocated"})
        elif args.command == "start":
            _print(
                {
                    "generation_id": generation_id,
                    "canvas_id": control.start(generation_id, actor=args.actor),
                }
            )
        elif args.command == "status":
            state = control._repository.get_generation(generation_id)
            _print(
                {
                    "generation": asdict(state),
                    "child_runs": [
                        asdict(run) for run in control._repository.list_child_runs(generation_id)
                    ],
                }
            )
        elif args.command == "request-stop":
            _print(
                {
                    "requested": control.request_stop(
                        generation_id,
                        actor=args.actor,
                        reason=args.reason,
                    )
                }
            )
        elif args.command == "resume":
            _print({"canvas_id": control.resume(generation_id, occurrence=args.occurrence)})
        elif args.command == "reconcile":
            _print({"reconciliation_digest": control.reconcile(generation_id, actor=args.actor)})
        elif args.command == "freeze":
            control.freeze(generation_id, reconciliation_digest=args.reconciliation_digest)
            _print({"generation_id": generation_id, "status": "frozen"})
        elif args.command == "qualify":
            raw_secret = os.environ.get(args.artifact_signing_key_env)
            if raw_secret is None:
                raise ValueError("artifact signing key environment is missing")
            configuration = ArtifactStoreConfiguration(
                primary_root=args.artifact_primary_root,
                backup_root=args.artifact_backup_root,
                signing_key_id=args.artifact_signing_key_id,
                signing_key_secret=decode_signing_secret(raw_secret),
                retained_verification_keys=retained_keys_from_environment(
                    args.artifact_retained_key_env
                ),
            )
            with configuration.open() as store:
                result = control.qualify_from_artifacts(
                    generation_id,
                    store,
                    owner_artifact_id=args.owner_artifact_id,
                    stage_artifact_id=args.stage_artifact_id,
                )
            _print(
                {
                    "generation_id": generation_id,
                    "status": "qualified",
                    "evidence_digest": result.evidence_digest,
                }
            )
        elif args.command == "accept":
            control.accept(generation_id, actor=args.actor, reason=args.reason)
            _print({"generation_id": generation_id, "status": "accepted"})
        elif args.command == "reject":
            control.reject(
                generation_id,
                actor=args.actor,
                reason=args.reason,
                remediation=args.remediation,
            )
            _print({"generation_id": generation_id, "status": "rejected", "dispatch_blocked": True})
        elif args.command == "activate":
            manifest = load_inventory(args.manifest)
            canvas_id = control.activate(
                corrective_generation_id=generation_id,
                successor_generation_id=args.successor_generation_id,
                manifest=manifest,
                successor_boundary_digest=args.successor_boundary_digest,
                occurrence=args.occurrence,
                actor=args.actor,
            )
            _print(
                {
                    "successor_generation_id": args.successor_generation_id,
                    "status": "active",
                    "canvas_id": canvas_id,
                }
            )
        elif args.command == "recover-successor":
            manifest = load_inventory(args.manifest)
            canvas_id = control.recover_successor(
                corrective_generation_id=generation_id,
                failed_successor_generation_id=args.failed_successor_generation_id,
                replacement_successor_generation_id=args.replacement_successor_generation_id,
                manifest=manifest,
                successor_boundary_digest=args.successor_boundary_digest,
                occurrence=args.occurrence,
                actor=args.actor,
                reason=args.reason,
            )
            _print(
                {
                    "superseded_successor_generation_id": args.failed_successor_generation_id,
                    "replacement_successor_generation_id": (
                        args.replacement_successor_generation_id
                    ),
                    "status": "active",
                    "canvas_id": canvas_id,
                }
            )
        elif args.command == "verify-tail":
            tail_result = control._repository.verify_tail(
                corrective_generation_id=generation_id,
                successor_generation_id=args.successor_generation_id,
            )
            _print({**asdict(tail_result), "passed": tail_result.passed})
            if not tail_result.passed:
                return 2
        else:
            rollback = control.rollback_status(
                generation_id,
                successor_generation_id=args.successor_generation_id,
            )
            _print(asdict(rollback))
        return 0
    finally:
        client.close()


def _generation_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--generation-id", required=True)


def _required_text(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _optional_text(value: JsonValue | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional inventory text must be null or non-empty")
    return value


def _required_int(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _required_bool(payload: dict[str, JsonValue], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _required_list(payload: dict[str, JsonValue], key: str) -> list[JsonValue]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _runtime_category_mapping(
    runtime_config: BitrixOpenLinesConfig,
) -> tuple[tuple[str, ...], dict[str, str]]:
    categories = tuple(runtime_config.included_crm_category_ids)
    if not categories:
        raise ValueError("deployed runtime configuration must include CRM categories")
    mapping = {
        category_id: runtime_config.entity_by_crm_category_id[category_id]
        for category_id in categories
        if category_id in runtime_config.entity_by_crm_category_id
    }
    if len(mapping) != len(categories):
        raise ValueError("deployed runtime configuration has incomplete category mappings")
    return categories, mapping


def _require_new_generation_configuration(expected_digest: str) -> None:
    runtime_config = get_ingestion_config().bitrix_openlines
    categories, _mapping = _runtime_category_mapping(runtime_config)
    if bitrix_configuration_digest(runtime_config, categories) != expected_digest:
        raise ValueError("new generation configuration digest does not match the deployed runtime")


def _require_runtime_configuration(
    expected_digest: str,
    *,
    generation_id: str,
    repository: BitrixBackfillRepository,
) -> None:
    runtime_config = get_ingestion_config().bitrix_openlines
    categories, runtime_mapping = _runtime_category_mapping(runtime_config)
    runtime_digest = bitrix_configuration_digest(runtime_config, categories)
    if runtime_digest == expected_digest:
        return
    legacy_digest = bitrix_legacy_explicit_category_digest(runtime_config, categories)
    if legacy_digest != expected_digest:
        raise RuntimeError(
            "deployed container ingestion config does not match the accepted generation"
        )
    accepted_mapping = repository.get_generation_category_mapping(generation_id)
    if accepted_mapping != runtime_mapping:
        raise RuntimeError(
            "deployed runtime configuration does not match generation category mapping evidence"
        )


def _digest_text(domain: str, value: str) -> str:
    encoded = value.encode("utf-8")
    return "sha256:" + hashlib.sha256(domain.encode("ascii") + b"\x00" + encoded).hexdigest()


def _print(payload: object) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main() -> None:
    try:
        raise SystemExit(run())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"bitrix backfill control failed: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
