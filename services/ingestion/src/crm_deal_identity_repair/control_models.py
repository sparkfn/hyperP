"""Strict non-mutating control-plane values owned by repair quiescence (#310)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from src.crm_deal_identity_repair.execution_boundary_models import (
    RepairBoundarySnapshot,
    _nonempty,
    _validate_digest,
)

RepairControlState = Literal[
    "qualified",
    "quiescing",
    "quiesced",
    "allocated",
    "paused",
    "lost",
]
RepairControlPriorState = Literal["quiesced", "allocated"]
RepairOverlayDisposition = Literal["executable", "blocked", "investigate"]


@dataclass(frozen=True)
class RepairControlLease:
    """CAS-owned dispatch block; it is never a release or execution authority."""

    run_id: str
    owner_id: str
    token: str
    revision: int
    state: RepairControlState
    boundary_digest: str
    prior_state: RepairControlPriorState | None = None

    def __post_init__(self) -> None:
        _nonempty(self.run_id, "repair control run ID")
        _nonempty(self.owner_id, "repair control owner")
        _nonempty(self.token, "repair control token")
        _positive(self.revision, "repair control revision")
        _validate_digest(self.boundary_digest, "repair control boundary digest")
        if self.state not in {"qualified", "quiescing", "quiesced", "allocated", "paused", "lost"}:
            raise ValueError("repair control state is invalid")
        if self.state == "paused" and self.prior_state not in {"quiesced", "allocated"}:
            raise ValueError("paused repair control requires a resumable prior state")
        if self.state != "paused" and self.prior_state is not None:
            raise ValueError("non-paused repair control cannot retain a prior state")


@dataclass(frozen=True)
class RepairLogicalRunCapture:
    """Immutable identity and current state for one captured logical run."""

    logical_run_id: str
    status: str

    def __post_init__(self) -> None:
        _nonempty(self.logical_run_id, "captured logical run ID")
        _nonempty(self.status, "captured logical run status")


@dataclass(frozen=True)
class RepairIngestRunCapture:
    """Immutable identity and current state for one captured ingest attempt."""

    ingest_run_id: str
    status: str
    generation: int

    def __post_init__(self) -> None:
        _nonempty(self.ingest_run_id, "captured ingest run ID")
        _nonempty(self.status, "captured ingest run status")
        _nonnegative(self.generation, "captured ingest run generation")


@dataclass(frozen=True)
class RepairCheckpointCapture:
    """Exact composite identity and state for one captured checkpoint."""

    logical_run_id: str
    phase: str
    generation: int
    status: str

    def __post_init__(self) -> None:
        _nonempty(self.logical_run_id, "captured checkpoint logical run ID")
        _nonempty(self.phase, "captured checkpoint phase")
        _nonnegative(self.generation, "captured checkpoint generation")
        _nonempty(self.status, "captured checkpoint status")


@dataclass(frozen=True)
class RepairStreamCapture:
    """Exact active-fence identity for one captured Bitrix stream."""

    stream_key: str
    logical_run_id: str
    ingest_run_id: str
    attempt_generation: int
    stream_generation: int
    fencing_token: int
    status: str

    def __post_init__(self) -> None:
        _nonempty(self.stream_key, "captured stream key")
        _nonempty(self.logical_run_id, "captured stream logical run ID")
        _nonempty(self.ingest_run_id, "captured stream ingest run ID")
        _nonnegative(self.attempt_generation, "captured stream attempt generation")
        _nonnegative(self.stream_generation, "captured stream generation")
        _nonnegative(self.fencing_token, "captured stream fencing token")
        _nonempty(self.status, "captured stream status")


@dataclass(frozen=True)
class RepairGenerationCapture:
    """Immutable identity and current state for one captured backfill generation."""

    generation_id: str
    status: str

    def __post_init__(self) -> None:
        _nonempty(self.generation_id, "captured backfill generation ID")
        _nonempty(self.status, "captured backfill generation status")


@dataclass(frozen=True)
class RepairPublicationCapture:
    """Exact identity and current state for one captured publication outbox row."""

    successor_generation_id: str
    evidence_digest: str
    occurrence: str
    status: str

    def __post_init__(self) -> None:
        _nonempty(self.successor_generation_id, "captured publication generation ID")
        _nonempty(self.evidence_digest, "captured publication evidence digest")
        _nonempty(self.occurrence, "captured publication occurrence")
        _nonempty(self.status, "captured publication status")


@dataclass(frozen=True)
class RepairTopologyCapture:
    """Frozen affected deal/activity/Open Lines topology for one quiescence CAS."""

    logical_run_ids: tuple[RepairLogicalRunCapture, ...]
    ingest_run_ids: tuple[RepairIngestRunCapture, ...]
    checkpoint_ids: tuple[RepairCheckpointCapture, ...]
    stream_ids: tuple[RepairStreamCapture, ...]
    generation_ids: tuple[RepairGenerationCapture, ...]
    publication_ids: tuple[RepairPublicationCapture, ...]

    def as_parameters(self) -> Mapping[str, object]:
        """Return exact serializable identities without recomputing the snapshot."""
        parameters: dict[str, object] = {
            "logical_run_ids": [
                {"logical_run_id": item.logical_run_id, "status": item.status}
                for item in self.logical_run_ids
            ],
            "ingest_run_ids": [
                {
                    "ingest_run_id": item.ingest_run_id,
                    "status": item.status,
                    "generation": item.generation,
                }
                for item in self.ingest_run_ids
            ],
            "checkpoint_ids": [
                {
                    "logical_run_id": item.logical_run_id,
                    "phase": item.phase,
                    "generation": item.generation,
                    "status": item.status,
                }
                for item in self.checkpoint_ids
            ],
            "stream_ids": [
                {
                    "stream_key": item.stream_key,
                    "logical_run_id": item.logical_run_id,
                    "ingest_run_id": item.ingest_run_id,
                    "attempt_generation": item.attempt_generation,
                    "stream_generation": item.stream_generation,
                    "fencing_token": item.fencing_token,
                    "status": item.status,
                }
                for item in self.stream_ids
            ],
            "generation_ids": [
                {"generation_id": item.generation_id, "status": item.status}
                for item in self.generation_ids
            ],
            "publication_ids": [
                {
                    "successor_generation_id": item.successor_generation_id,
                    "evidence_digest": item.evidence_digest,
                    "occurrence": item.occurrence,
                    "status": item.status,
                }
                for item in self.publication_ids
            ],
        }
        return parameters


@dataclass(frozen=True)
class RepairOverlayRow:
    """One sealed approval decision over one immutable qualified inventory row."""

    inventory_key: str
    source_record_pk: str
    inventory_fingerprint: str
    disposition: RepairOverlayDisposition

    def __post_init__(self) -> None:
        _nonempty(self.inventory_key, "overlay inventory key")
        _nonempty(self.source_record_pk, "overlay source-record PK")
        _validate_digest(self.inventory_fingerprint, "overlay inventory fingerprint")
        if self.disposition not in {"executable", "blocked", "investigate"}:
            raise ValueError("repair overlay disposition is invalid")


@dataclass(frozen=True)
class RepairAllocationCompletion:
    """Sealed allocation result, including the meaningful zero-unit case."""

    run_id: str
    allocation_digest: str
    executable_count: int
    unit_count: int

    def __post_init__(self) -> None:
        _nonempty(self.run_id, "allocation completion run ID")
        _validate_digest(self.allocation_digest, "allocation completion digest")
        _nonnegative(self.executable_count, "allocation executable count")
        _nonnegative(self.unit_count, "allocation unit count")
        if self.executable_count != self.unit_count:
            raise ValueError("allocation completion counts are inconsistent")


@dataclass(frozen=True)
class RepairStaleRunProof:
    """Exact non-domain ownership evidence required to fail one stale ingest run."""

    ingest_run_id: str
    control_instance_id: str
    source_key: str
    status: str
    logical_run_ids: tuple[str, ...]
    checkpoint_ids: tuple[str, ...]
    stream_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        _nonempty(self.ingest_run_id, "stale ingest run ID")
        _nonempty(self.control_instance_id, "stale run control instance")
        _nonempty(self.source_key, "stale run source key")
        _nonempty(self.status, "stale run status")
        _unique_nonempty(self.logical_run_ids, "stale run logical IDs")
        _unique_nonempty(self.checkpoint_ids, "stale run checkpoint IDs")
        _unique_nonempty(self.stream_keys, "stale run stream keys")
        if len(self.logical_run_ids) > 1:
            raise ValueError("stale run has ambiguous logical ownership")

    @property
    def is_orphan(self) -> bool:
        return not self.logical_run_ids and not self.checkpoint_ids and not self.stream_keys

    def as_parameters(self) -> Mapping[str, object]:
        return {
            "stale_run_id": self.ingest_run_id,
            "stale_control_instance_id": self.control_instance_id,
            "stale_source_key": self.source_key,
            "stale_status": self.status,
            "logical_run_ids": list(self.logical_run_ids),
            "checkpoint_ids": list(self.checkpoint_ids),
            "stream_keys": list(self.stream_keys),
        }


@dataclass(frozen=True)
class RepairBoundaryComponentProof:
    """#310-derived baseline/post proof; it preserves the immutable #300 aggregate contract."""

    source_instance_id: str
    control_instance_id: str
    inventory_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int
    source_records_digest: str
    source_instance_digest: str
    control_digest: str
    stale_run_evidence_digest: str

    def __post_init__(self) -> None:
        _nonempty(self.source_instance_id, "boundary proof source instance")
        _nonempty(self.control_instance_id, "boundary proof control instance")
        for value, label in (
            (self.inventory_digest, "boundary proof inventory"),
            (self.source_records_digest, "boundary proof source records"),
            (self.source_instance_digest, "boundary proof source instance"),
            (self.control_digest, "boundary proof control"),
            (self.stale_run_evidence_digest, "boundary proof stale run"),
        ):
            _validate_digest(value, label)
        _nonnegative(self.inventory_row_count, "boundary proof inventory count")
        _nonnegative(self.eligible_unit_count, "boundary proof eligible count")
        _nonnegative(self.negative_control_count, "boundary proof negative-control count")

    @classmethod
    def from_snapshot(cls, snapshot: RepairBoundarySnapshot) -> RepairBoundaryComponentProof:
        """Copy only validated component evidence from a #300 snapshot."""
        return cls(
            source_instance_id=snapshot.source_instance_id,
            control_instance_id=snapshot.control_instance_id,
            inventory_digest=snapshot.inventory_digest,
            inventory_row_count=snapshot.inventory_row_count,
            eligible_unit_count=snapshot.eligible_unit_count,
            negative_control_count=snapshot.negative_control_count,
            source_records_digest=snapshot.source_records_digest,
            source_instance_digest=snapshot.source_instance_digest,
            control_digest=snapshot.control_digest,
            stale_run_evidence_digest=snapshot.stale_run_evidence_digest,
        )

    def immutable_matches(self, other: RepairBoundaryComponentProof) -> bool:
        """Control and stale evidence evolve only through separately persisted #310 proof."""
        return (
            self.source_instance_id == other.source_instance_id
            and self.control_instance_id == other.control_instance_id
            and self.inventory_digest == other.inventory_digest
            and self.inventory_row_count == other.inventory_row_count
            and self.eligible_unit_count == other.eligible_unit_count
            and self.negative_control_count == other.negative_control_count
            and self.source_records_digest == other.source_records_digest
            and self.source_instance_digest == other.source_instance_digest
        )


@dataclass(frozen=True)
class RepairControlStatus:
    """Read-only control-plane status; it never supplies execution authority."""

    run_id: str
    boundary_digest: str
    owner_id: str | None
    revision: int | None
    state: RepairControlState | None
    prior_state: RepairControlPriorState | None
    allocation_digest: str | None
    allocation_unit_count: int
    completion_unit_count: int | None
    dispatch_blocked: bool | None
    dispatch_owner_id: str | None
    topology_active_count: int
    topology_superseded_count: int
    stale_run_proof_count: int
    task_proof_state: str | None
    stop_reason: str | None

    def __post_init__(self) -> None:
        _nonempty(self.run_id, "repair status run ID")
        _validate_digest(self.boundary_digest, "repair status boundary digest")
        _nonnegative(self.allocation_unit_count, "repair status allocation count")
        _nonnegative(self.topology_active_count, "repair status active topology count")
        _nonnegative(self.topology_superseded_count, "repair status superseded topology count")
        _nonnegative(self.stale_run_proof_count, "repair status stale proof count")


def _unique_nonempty(values: tuple[str, ...], label: str) -> None:
    if any(not value for value in values) or len(set(values)) != len(values):
        raise ValueError(f"{label} must be unique non-empty values")


def _positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be positive")


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be non-negative")
