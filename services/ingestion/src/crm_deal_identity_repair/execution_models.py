"""Stable aggregation surface for non-executable CRM repair contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.crm_deal_identity_repair.execution_boundary_models import (
    RepairBoundaryDriftReason,
    RepairBoundarySnapshot,
    RepairExecutionBoundaryManifest,
    RepairStopCondition,
)
from src.crm_deal_identity_repair.execution_records import (
    RepairCheckpoint,
    RepairCheckpointState,
    RepairFence,
    RepairFenceState,
    RepairMutationOutcome,
    RepairMutationResult,
    RepairOutboxEvent,
    RepairOutboxState,
    RepairQuiescence,
    RepairQuiescenceState,
    RepairRollbackImage,
    RepairRollbackState,
    RepairSecondaryDisposition,
    RepairSecondaryOutcome,
    RepairUnit,
    RepairUnitState,
    RepairVerificationOutcome,
    RepairVerificationResult,
)
from src.crm_deal_identity_repair.execution_status_models import (
    RepairQualificationRun,
    RepairQualificationState,
    RepairRunStatus,
    RepairStatusReason,
)

__all__ = (
    "RepairBoundarySnapshot",
    "RepairBoundaryDriftReason",
    "RepairCheckpoint",
    "RepairCheckpointState",
    "RepairExecutionBoundaryManifest",
    "RepairFence",
    "RepairFenceState",
    "RepairMutationOutcome",
    "RepairMutationResult",
    "RepairOutboxEvent",
    "RepairOutboxState",
    "RepairQualificationRun",
    "RepairQualificationState",
    "RepairQuiescence",
    "RepairQuiescenceState",
    "RepairRollbackImage",
    "RepairRollbackState",
    "RepairRunStatus",
    "RepairStatusReason",
    "RepairSecondaryDisposition",
    "RepairSecondaryOutcome",
    "RepairStopCondition",
    "RepairUnit",
    "RepairUnitState",
    "RepairVerificationOutcome",
    "RepairVerificationResult",
    "RepairAtomicVerificationResult",
    "RepairRunEquationCommand",
    "RepairRunEquationResult",
    "RepairSecondaryAction",
    "RepairSecondarySubject",
    "RepairSecondarySubjectKind",
    "RepairUnitEquation",
    "RepairVerificationCommand",
)

if TYPE_CHECKING:
    from src.crm_deal_identity_repair.verification_models import (
        RepairAtomicVerificationResult,
        RepairRunEquationCommand,
        RepairRunEquationResult,
        RepairSecondaryAction,
        RepairSecondarySubject,
        RepairSecondarySubjectKind,
        RepairUnitEquation,
        RepairVerificationCommand,
    )


def __getattr__(name: str) -> object:
    """Lazily expose #311 contracts without introducing a #309 import cycle."""
    if name in {
        "RepairAtomicVerificationResult",
        "RepairRunEquationCommand",
        "RepairRunEquationResult",
        "RepairSecondaryAction",
        "RepairSecondarySubject",
        "RepairSecondarySubjectKind",
        "RepairUnitEquation",
        "RepairVerificationCommand",
    }:
        from src.crm_deal_identity_repair import verification_models

        return getattr(verification_models, name)
    raise AttributeError(name)
