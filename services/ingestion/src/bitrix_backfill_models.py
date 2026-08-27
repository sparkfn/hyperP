"""Typed contracts for corrective Bitrix generation topology and coverage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from src.bitrix_ingestion_models import BitrixStreamKey
from src.models import JsonValue
from src.resumable import CheckpointDescriptor
from src.source_instances import LEGACY_DEFAULT_CONTROL_INSTANCE_ID, effective_control_instance_id

KNOWN_OWNER_REFRESH_CONNECTOR_VERSION = "bitrix-crm-known-owner-refresh-v1"

GenerationStatus = Literal[
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
]
CoverageDisposition = Literal[
    "created",
    "existing_same_hash",
    "updated_projection",
    "scope_unchanged",
    "excluded_out_of_scope",
    "quarantined_owner_unresolved",
    "conflict",
    "failed",
]


@dataclass(frozen=True)
class GenerationProvenance:
    """Immutable provenance required before a corrective generation is allocated."""

    repository_sha: str
    image_digest: str
    configuration_digest: str
    source_contract_uuid: str
    boundary_digest: str

    def __post_init__(self) -> None:
        for name, value in (
            ("repository_sha", self.repository_sha),
            ("image_digest", self.image_digest),
            ("configuration_digest", self.configuration_digest),
            ("source_contract_uuid", self.source_contract_uuid),
            ("boundary_digest", self.boundary_digest),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class GenerationRunContext:
    """Corrective-generation identity carried by one child split run."""

    generation_id: str
    boundary_digest: str
    configuration_digest: str
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID

    def __post_init__(self) -> None:
        if not all(value.strip() for value in vars(self).values()):
            raise ValueError("generation run identity values must be non-empty")
        object.__setattr__(
            self,
            "control_instance_id",
            effective_control_instance_id(self.control_instance_id),
        )


@dataclass(frozen=True)
class KnownOwnerMembershipSet:
    generation_id: str
    membership_set_id: str
    digest: str
    deal_ids: tuple[str, ...]
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "control_instance_id",
            effective_control_instance_id(self.control_instance_id),
        )


@dataclass(frozen=True)
class CoverageReconciliation:
    stream_key: BitrixStreamKey
    coverage_count: int
    terminal_count: int
    created_count: int
    duplicate_count: int
    projection_count: int
    unchanged_count: int
    excluded_count: int
    quarantine_count: int
    conflict_count: int
    failed_count: int
    checkpoint_committed_count: int
    checkpoint_duplicate_count: int
    checkpoint_excluded_count: int
    checkpoint_retry_count: int

    @property
    def complete(self) -> bool:
        accounted = (
            self.created_count
            + self.duplicate_count
            + self.projection_count
            + self.unchanged_count
            + self.excluded_count
            + self.quarantine_count
            + self.conflict_count
            + self.failed_count
        )
        return (
            self.coverage_count == self.terminal_count == accounted
            and self.conflict_count == 0
            and self.failed_count == 0
            and self.checkpoint_committed_count == self.created_count + self.projection_count
            and self.checkpoint_duplicate_count == self.duplicate_count + self.unchanged_count
            and self.checkpoint_excluded_count
            == self.excluded_count + self.quarantine_count + self.conflict_count
            and self.checkpoint_retry_count == self.failed_count
        )


@dataclass(frozen=True)
class CoverageEntry:
    """One terminal source identity accounting row."""

    source_identity: str
    source_boundary: str
    disposition: CoverageDisposition
    source_observation_hash: str
    terminal: bool = True
    deal_id: str | None = None
    scope_state: str | None = None
    entity_key: str | None = None
    category_id: str | None = None
    stage_id: str | None = None
    census_epoch: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.source_identity.strip() or not self.source_boundary.strip():
            raise ValueError("coverage identity and boundary must be non-empty")
        if not self.source_observation_hash.strip():
            raise ValueError("coverage source observation hash must be non-empty")
        if self.census_epoch is not None and (
            isinstance(self.census_epoch, bool) or self.census_epoch < 1
        ):
            raise ValueError("coverage census_epoch must be positive")

    @property
    def outcome_digest(self) -> str:
        payload = {
            "source_identity": self.source_identity,
            "source_boundary": self.source_boundary,
            "disposition": self.disposition,
            "source_observation_hash": self.source_observation_hash,
            "terminal": self.terminal,
            "deal_id": self.deal_id,
            "scope_state": self.scope_state,
            "entity_key": self.entity_key,
            "category_id": self.category_id,
            "stage_id": self.stage_id,
            "census_epoch": self.census_epoch,
            "detail": self.detail,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(b"bitrix-coverage-outcome-v1\x00" + encoded).hexdigest()


def initial_stream_checkpoint(
    stream_key: BitrixStreamKey,
    *,
    source_window: dict[str, JsonValue],
    census_epoch: int = 1,
) -> CheckpointDescriptor:
    """Build the fixed version-1 checkpoint contract for a child stream."""
    if stream_key == "crm_stage_history":
        raise ValueError("stage history uses standalone artifact replay checkpoints")
    validate_stream_source_window(stream_key, source_window)
    if stream_key == "crm_deals":
        phase = "scoped_deal_census_v1"
        cursor: dict[str, JsonValue] = {
            "last_deal_id": None,
            "census_epoch": census_epoch,
        }
        connector = "bitrix-crm-deals-keyset-v1"
        boundary = "exclusive_last_deal_id"
    elif stream_key == "crm_activities":
        phase = "crm_activity_keyset_v1"
        cursor = {"last_activity_id": None}
        connector = "bitrix-crm-activity-keyset-v1"
        boundary = "exclusive_last_activity_id"
    else:
        phase = "openlines_conversation_replay_v1"
        cursor = {"crm_start": None}
        connector = "bitrix-openlines-replay-v1"
        boundary = "at_least_once_page_start"
    return CheckpointDescriptor(
        phase=phase,
        cursor=cursor,
        source_window=dict(source_window),
        last_committed_record_id=None,
        connector_version=connector,
        schema_version=1,
        replay_boundary=boundary,
    )


def validate_stream_source_window(
    stream_key: BitrixStreamKey,
    source_window: dict[str, JsonValue],
) -> None:
    """Reject incomplete or ambiguous corrective source-window boundaries."""
    if stream_key == "crm_stage_history":
        raise ValueError("stage history is not part of a Bitrix backfill generation")
    required = {
        "crm_deals": {"upper_deal_id", "included_category_digest", "owner_artifact_id"},
        "crm_activities": {"upper_activity_id", "owner_artifact_id"},
        "openlines_conversations": {
            "discovery_boundary_digest",
            "selected_config_digest",
        },
    }[stream_key]
    if set(source_window) != required:
        raise ValueError(f"{stream_key} source window must contain exactly {sorted(required)}")
    for key, value in source_window.items():
        if key == "owner_artifact_id" and value is None:
            continue
        if not isinstance(value, (str, int)) or isinstance(value, bool) or str(value).strip() == "":
            raise ValueError(f"{stream_key} source window contains an invalid {key}")


def known_owner_refresh_checkpoint(
    membership: KnownOwnerMembershipSet,
    *,
    census_epoch: int,
) -> CheckpointDescriptor:
    return CheckpointDescriptor(
        phase="known_owner_refresh_v1",
        cursor={"last_known_deal_id": None, "census_epoch": census_epoch},
        source_window={
            "known_owner_membership_set_id": membership.membership_set_id,
            "known_owner_set_digest": membership.digest,
            "known_owner_count": len(membership.deal_ids),
        },
        last_committed_record_id=None,
        connector_version=KNOWN_OWNER_REFRESH_CONNECTOR_VERSION,
        schema_version=1,
        replay_boundary="exclusive_sorted_known_deal_id",
    )


InventoryReplayMode = Literal[
    "strict_keyset",
    "fixed_keyset",
    "targeted_refresh",
    "bounded_replay",
    "excluded",
]
RollbackClass = Literal[
    "pre_write_image_rollback",
    "post_activation_pre_write_supersession",
    "post_write_compensation_or_restore",
]


@dataclass(frozen=True)
class BackfillInventoryEntry:
    """One reviewed Bitrix gap and its bounded completion contract."""

    gap_id: str
    stream_key: BitrixStreamKey
    bounded_population: int
    current_count: int
    source_basis: str
    expected_repair: str
    replay_mode: InventoryReplayMode
    source_window: dict[str, JsonValue] | None
    completion_equation: str
    max_calls: int
    max_rows: int
    max_runtime_seconds: int
    max_storage_bytes: int
    max_lock_seconds: int
    max_lag_seconds: int
    rollback_path: str
    reviewed_exclusion: str | None = None

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.gap_id,
                self.source_basis,
                self.expected_repair,
                self.completion_equation,
                self.rollback_path,
            )
        ):
            raise ValueError("inventory text fields must be non-empty")
        values = (
            self.bounded_population,
            self.current_count,
            self.max_calls,
            self.max_rows,
            self.max_runtime_seconds,
            self.max_storage_bytes,
            self.max_lock_seconds,
            self.max_lag_seconds,
        )
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("inventory counts and ceilings must be non-negative")
        if self.current_count > self.bounded_population:
            raise ValueError("inventory current count cannot exceed bounded population")
        if self.replay_mode == "excluded":
            if self.reviewed_exclusion is None or not self.reviewed_exclusion.strip():
                raise ValueError("excluded inventory entries require reviewed_exclusion")
            if self.source_window is not None:
                raise ValueError("excluded inventory entries cannot dispatch a source window")
        else:
            if any(
                value < 1
                for value in (
                    self.max_calls,
                    self.max_rows,
                    self.max_runtime_seconds,
                    self.max_storage_bytes,
                    self.max_lock_seconds,
                    self.max_lag_seconds,
                )
            ):
                raise ValueError("executed inventory ceilings must be positive")
            if self.bounded_population > self.max_rows:
                raise ValueError("bounded population exceeds the approved row ceiling")
            if self.reviewed_exclusion is not None:
                raise ValueError("executed inventory entries cannot have reviewed_exclusion")
            if self.source_window is None:
                raise ValueError("executed inventory entries require a source window")
            validate_stream_source_window(self.stream_key, self.source_window)

    @property
    def executes(self) -> bool:
        return self.replay_mode != "excluded"


@dataclass(frozen=True)
class BackfillInventoryManifest:
    """Human-reviewed complete Bitrix gap inventory."""

    source_key: str
    reviewed_by: str
    backup_id: str
    backup_restore_evidence_digest: str
    minimum_fence_image_digest: str
    legacy_dispatch_paused: bool
    predecessor_quiescent: bool
    entries: tuple[BackfillInventoryEntry, ...]

    def __post_init__(self) -> None:
        if self.source_key != "bitrix_chat":
            raise ValueError("corrective inventory must be Bitrix-only")
        for value in (
            self.reviewed_by,
            self.backup_id,
            self.backup_restore_evidence_digest,
            self.minimum_fence_image_digest,
        ):
            if not value.strip():
                raise ValueError("inventory prerequisite evidence must be non-empty")
        if not self.legacy_dispatch_paused or not self.predecessor_quiescent:
            raise ValueError("legacy dispatch and predecessor activity must be quiescent")
        if not self.entries:
            raise ValueError("inventory must contain at least one reviewed gap")
        gap_ids = [entry.gap_id for entry in self.entries]
        if len(set(gap_ids)) != len(gap_ids):
            raise ValueError("inventory gap IDs must be unique")
        executed = [entry.stream_key for entry in self.entries if entry.executes]
        if "crm_deals" not in executed:
            raise ValueError("inventory must execute the deal stream")
        if "crm_activities" in executed and executed.index("crm_deals") > executed.index(
            "crm_activities"
        ):
            raise ValueError("deal inventory must precede activity inventory")
        activity_entries = [entry for entry in self.entries if entry.stream_key == "crm_activities"]
        if not activity_entries:
            raise ValueError("inventory must review the activity stream")

    @property
    def canonical_json(self) -> str:
        payload = {
            "source_key": self.source_key,
            "reviewed_by": self.reviewed_by,
            "backup_id": self.backup_id,
            "backup_restore_evidence_digest": self.backup_restore_evidence_digest,
            "minimum_fence_image_digest": self.minimum_fence_image_digest,
            "legacy_dispatch_paused": self.legacy_dispatch_paused,
            "predecessor_quiescent": self.predecessor_quiescent,
            "entries": [vars(entry) for entry in self.entries],
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @property
    def digest(self) -> str:
        encoded = self.canonical_json.encode("utf-8")
        return "sha256:" + hashlib.sha256(b"bitrix-backfill-inventory-v1\x00" + encoded).hexdigest()

    @property
    def executable_entries(self) -> tuple[BackfillInventoryEntry, ...]:
        return tuple(entry for entry in self.entries if entry.executes)


@dataclass(frozen=True)
class GenerationState:
    generation_id: str
    status: GenerationStatus
    generation_kind: str
    inventory_digest: str | None
    corrective_generation_id: str | None
    frozen_at: str | None
    material_write_count: int
    repository_sha: str
    image_digest: str
    configuration_digest: str
    boundary_digest: str
    source_contract_uuid: str
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "control_instance_id",
            effective_control_instance_id(self.control_instance_id),
        )


@dataclass(frozen=True)
class QualificationResult:
    owner_artifact_id: str
    stage_artifact_id: str
    owner_recommendation: str
    stage_recommendation: str
    replay_digest: str
    stage_domain_writes: int

    def __post_init__(self) -> None:
        if self.owner_recommendation != "verified_keyset":
            raise ValueError("owner artifact is not verified_keyset")
        if self.stage_recommendation != "bounded_spool_reconcile":
            raise ValueError("stage artifact is not bounded_spool_reconcile")
        if self.stage_domain_writes != 0:
            raise ValueError("qualification detected forbidden stage-domain writes")
        if not all(
            value.strip()
            for value in (self.owner_artifact_id, self.stage_artifact_id, self.replay_digest)
        ):
            raise ValueError("qualification evidence identifiers must be non-empty")

    @property
    def evidence_digest(self) -> str:
        encoded = json.dumps(vars(self), sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(b"bitrix-qualification-result-v1\x00" + encoded).hexdigest()
        return "sha256:" + digest


@dataclass(frozen=True)
class RollbackStatus:
    rollback_class: RollbackClass
    dispatch_must_remain_blocked: bool
    required_action: str


@dataclass(frozen=True)
class TailVerification:
    corrective_status: str
    successor_status: str
    predecessor_frozen: bool
    expected_streams: tuple[BitrixStreamKey, ...]
    actual_streams: tuple[BitrixStreamKey, ...]
    cadence_run_count: int
    cadence_complete: bool
    successor_coverage_count: int
    coverage_complete: bool

    @property
    def passed(self) -> bool:
        return (
            self.corrective_status == "accepted"
            and self.successor_status == "active"
            and self.predecessor_frozen
            and len(self.actual_streams) == len(self.expected_streams)
            and set(self.actual_streams) == set(self.expected_streams)
            and self.cadence_run_count > 0
            and self.cadence_complete
            and self.successor_coverage_count > 0
            and self.coverage_complete
        )


@dataclass(frozen=True)
class GenerationChildRun:
    stream_key: BitrixStreamKey
    logical_run_id: str
    logical_status: str
    attempt_generation: int
    stream_status: str | None
    control_instance_id: str = LEGACY_DEFAULT_CONTROL_INSTANCE_ID

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "control_instance_id",
            effective_control_instance_id(self.control_instance_id),
        )
