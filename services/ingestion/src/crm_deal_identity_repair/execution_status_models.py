"""Qualification-run and read-only status contracts for repair boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.crm_deal_identity_repair.execution_boundary_models import (
    RepairBoundaryDriftReason,
    RepairExecutionBoundaryManifest,
    _nonempty,
    _validate_digest,
)

RepairQualificationState = Literal["qualified", "drifted"]
RepairStatusReason = Literal[
    "not_qualified",
    "exact_boundary_match",
    "boundary_not_observed",
    "missing_source_record",
    "source_instance_mismatch",
    "source_instance_disabled",
    "missing_binding",
    "missing_control_evidence",
    "binding_mismatch",
    "persisted_boundary_change",
]
_DRIFT_REASONS = frozenset(
    {
        "boundary_not_observed",
        "missing_source_record",
        "source_instance_mismatch",
        "source_instance_disabled",
        "missing_binding",
        "missing_control_evidence",
        "binding_mismatch",
        "persisted_boundary_change",
    }
)


@dataclass(frozen=True)
class RepairQualificationRun:
    """Persisted qualification identity with its complete immutable manifest."""

    repair_id: str
    run_id: str
    qualification_identity: str
    manifest: RepairExecutionBoundaryManifest
    boundary_digest: str
    status: Literal["qualified"]

    def __post_init__(self) -> None:
        _nonempty(self.repair_id, "repair ID")
        _nonempty(self.run_id, "run ID")
        _validate_digest(self.qualification_identity, "qualification identity")
        _validate_digest(self.boundary_digest, "boundary digest")
        if self.status != "qualified":
            raise ValueError("repair qualification run status is invalid")
        if self.manifest.repair_id != self.repair_id:
            raise ValueError("repair qualification run repair ID is inconsistent")
        if self.manifest.graph_boundary_digest != self.boundary_digest:
            raise ValueError("repair qualification run boundary digest is inconsistent")
        if self.qualification_identity != self.manifest.qualification_identity:
            raise ValueError("repair qualification identity is inconsistent")

    @property
    def manifest_digest(self) -> str:
        return self.manifest.manifest_digest

    @property
    def artifact_id(self) -> str:
        return self.manifest.artifact_id

    @property
    def artifact_manifest_hmac(self) -> str:
        return self.manifest.artifact_manifest_hmac

    @property
    def inventory_digest(self) -> str:
        return self.manifest.inventory_digest

    @property
    def source_instance_id(self) -> str:
        return self.manifest.source_instance_id

    @property
    def control_instance_id(self) -> str:
        return self.manifest.control_instance_id

    @property
    def inventory_row_count(self) -> int:
        return self.manifest.inventory_row_count

    @property
    def eligible_unit_count(self) -> int:
        return self.manifest.eligible_unit_count

    @property
    def negative_control_count(self) -> int:
        return self.manifest.negative_control_count


@dataclass(frozen=True)
class RepairRunStatus:
    """Read-only status; every qualified state exposes the full sealed boundary."""

    repair_id: str
    admissibility: Literal["admissible", "drifted", "not_qualified"]
    manifest: RepairExecutionBoundaryManifest | None
    qualification_identity: str | None
    expected_boundary_digest: str | None
    observed_boundary_digest: str | None
    reason_code: RepairStatusReason
    execution_allowed: Literal[False] = False

    def __post_init__(self) -> None:
        _nonempty(self.repair_id, "repair ID")
        if self.execution_allowed is not False:
            raise ValueError("repair status must remain non-executable")
        if self.admissibility == "not_qualified":
            _validate_not_qualified(self)
            return
        _validate_qualified_status(self)
        if self.admissibility == "admissible":
            _validate_admissible_status(self)
        elif self.reason_code not in _DRIFT_REASONS:
            raise ValueError("repair drift status reason is invalid")

    @classmethod
    def not_qualified(cls, repair_id: str) -> RepairRunStatus:
        return cls(repair_id, "not_qualified", None, None, None, None, "not_qualified")

    @classmethod
    def admissible(
        cls, run: RepairQualificationRun, observed_boundary_digest: str
    ) -> RepairRunStatus:
        return cls(
            run.repair_id,
            "admissible",
            run.manifest,
            run.qualification_identity,
            run.boundary_digest,
            observed_boundary_digest,
            "exact_boundary_match",
        )

    @classmethod
    def drifted(
        cls,
        run: RepairQualificationRun,
        reason_code: RepairBoundaryDriftReason,
        *,
        observed_boundary_digest: str | None = None,
    ) -> RepairRunStatus:
        return cls(
            run.repair_id,
            "drifted",
            run.manifest,
            run.qualification_identity,
            run.boundary_digest,
            observed_boundary_digest,
            reason_code,
        )

    @property
    def manifest_digest(self) -> str | None:
        return None if self.manifest is None else self.manifest.manifest_digest

    @property
    def source_instance_id(self) -> str | None:
        return None if self.manifest is None else self.manifest.source_instance_id

    @property
    def control_instance_id(self) -> str | None:
        return None if self.manifest is None else self.manifest.control_instance_id

    @property
    def inventory_row_count(self) -> int | None:
        return None if self.manifest is None else self.manifest.inventory_row_count

    @property
    def eligible_unit_count(self) -> int | None:
        return None if self.manifest is None else self.manifest.eligible_unit_count

    @property
    def negative_control_count(self) -> int | None:
        return None if self.manifest is None else self.manifest.negative_control_count


def _validate_not_qualified(status: RepairRunStatus) -> None:
    if status.reason_code != "not_qualified" or any(
        value is not None
        for value in (
            status.manifest,
            status.qualification_identity,
            status.expected_boundary_digest,
            status.observed_boundary_digest,
        )
    ):
        raise ValueError("not-qualified repair status has immutable boundary data")


def _validate_qualified_status(status: RepairRunStatus) -> None:
    if (
        status.manifest is None
        or status.qualification_identity is None
        or status.expected_boundary_digest is None
    ):
        raise ValueError("qualified repair status lacks immutable boundary data")
    _validate_digest(status.qualification_identity, "qualification identity")
    _validate_digest(status.expected_boundary_digest, "expected boundary digest")
    if status.observed_boundary_digest is not None:
        _validate_digest(status.observed_boundary_digest, "observed boundary digest")
    if status.manifest.repair_id != status.repair_id:
        raise ValueError("repair status manifest repair ID is inconsistent")
    if status.manifest.qualification_identity != status.qualification_identity:
        raise ValueError("repair status qualification identity is inconsistent")
    if status.manifest.graph_boundary_digest != status.expected_boundary_digest:
        raise ValueError("repair status expected boundary digest is inconsistent")


def _validate_admissible_status(status: RepairRunStatus) -> None:
    if (
        status.reason_code != "exact_boundary_match"
        or status.observed_boundary_digest != status.expected_boundary_digest
    ):
        raise ValueError("admissible repair status is inconsistent")
