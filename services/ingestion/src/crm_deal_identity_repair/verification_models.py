"""Strict contracts for read-only CRM repair verification and reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from src.crm_deal_identity_repair.digests import (
    disposition_digest,
    object_digest,
    outbox_claim_digest,
    verification_request_digest,
)
from src.crm_deal_identity_repair.execution_records import (
    RepairFence,
    RepairOutboxEvent,
    RepairSecondaryDisposition,
    RepairSecondaryOutcome,
    RepairUnit,
    RepairVerificationResult,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import (
    RepairMutationCommand,
    build_inventory_binding_digest,
)
from src.crm_deal_identity_repair.verification_equations import (
    RepairRunEquationCommand,
    RepairRunEquationResult,
    RepairUnitEquation,
)
from src.models import JsonValue

__all__ = (
    "RepairAtomicVerificationResult",
    "RepairRunEquationCommand",
    "RepairRunEquationResult",
    "RepairSecondaryAction",
    "RepairSecondaryDispositionDetail",
    "RepairSecondarySubject",
    "RepairSecondarySubjectKind",
    "RepairUnitEquation",
    "RepairVerificationCommand",
)


RepairSecondarySubjectKind = Literal[
    "crm_deal_count",
    "golden_profile",
    "survivorship_override",
    "pair_audit_case",
    "descendant",
    "merge_lineage",
    "no_match_lock",
    "identity_link_revision",
    "profile_analysis_invalidation",
]
RepairSecondaryAction = Literal[
    "recomputed",
    "preserved",
    "conflict_preserved",
    "cancelled_stale_pair",
    "rescored_pair",
    "appended_revision",
    "invalidated_once",
    "verified_exact",
    "no_op",
]
RepairVerificationDecision = Literal["committed", "replayed", "drift_conflict", "rejected"]

_SUBJECT_KINDS = frozenset(
    {
        "crm_deal_count",
        "golden_profile",
        "survivorship_override",
        "pair_audit_case",
        "descendant",
        "merge_lineage",
        "no_match_lock",
        "identity_link_revision",
        "profile_analysis_invalidation",
    }
)
_ACTIONS = frozenset(
    {
        "recomputed",
        "preserved",
        "conflict_preserved",
        "cancelled_stale_pair",
        "rescored_pair",
        "appended_revision",
        "invalidated_once",
        "verified_exact",
        "no_op",
    }
)


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"repair verification {label} must be non-negative")


@dataclass(frozen=True)
class RepairSecondarySubject:
    """A bounded secondary item; payloads deliberately contain no source evidence."""

    kind: RepairSecondarySubjectKind
    stable_id: str
    expected_digest: str
    mutation_id: str

    def __post_init__(self) -> None:
        if self.kind not in _SUBJECT_KINDS or not self.stable_id or not self.mutation_id:
            raise ValueError("repair verification secondary subject is invalid")
        if not self.expected_digest.startswith("sha256:"):
            raise ValueError("repair verification secondary subject digest is invalid")

    @property
    def fingerprint(self) -> str:
        return object_digest(
            b"crm-deal-identity-repair-verification-subject-v1\x00",
            {
                "kind": self.kind,
                "stable_id": self.stable_id,
                "expected_digest": self.expected_digest,
                "mutation_id": self.mutation_id,
            },
        )


@dataclass(frozen=True)
class RepairVerificationCommand:
    """One authenticated, fenced #309 mutation bundle verification request."""

    unit: RepairUnit
    fence: RepairFence
    inventory: RepairInventoryItem
    source_instance_id: str
    control_instance_id: str
    owner_id: str
    claim_token: str
    verification_contract_version: str = "crm_deal_identity_repair_verification_v1"

    def __post_init__(self) -> None:
        if self.inventory.partition == "negative_control":
            raise ValueError("negative-control inventory cannot become a verification command")
        mutation = RepairMutationCommand(
            self.unit, self.fence, self.inventory, self.source_instance_id, self.control_instance_id
        )
        if self.owner_id != self.fence.owner_id or not self.claim_token.strip():
            raise ValueError("verification owner or claim token is invalid")
        if self.verification_contract_version != "crm_deal_identity_repair_verification_v1":
            raise ValueError("verification contract version is invalid")
        if self.unit.inventory_binding_digest != build_inventory_binding_digest(self.inventory):
            raise ValueError("verification inventory binding differs")
        # Constructing the mutation command is intentional: it validates all #309 child identities.
        if not mutation.mutation_id:
            raise ValueError("verification mutation identity is invalid")

    @property
    def mutation_command(self) -> RepairMutationCommand:
        return RepairMutationCommand(
            self.unit, self.fence, self.inventory, self.source_instance_id, self.control_instance_id
        )

    @property
    def mutation_id(self) -> str:
        return self.mutation_command.mutation_id

    @property
    def rollback_image_id(self) -> str:
        return self.mutation_command.rollback_image_id

    @property
    def checkpoint_id(self) -> str:
        return self.mutation_command.checkpoint_id

    @property
    def outbox_event_id(self) -> str:
        return self.mutation_command.outbox_event_id

    @property
    def verification_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.request_digest + ":verification"))

    @property
    def request_digest(self) -> str:
        return verification_request_digest(self.to_dict())

    @property
    def claim_digest(self) -> str:
        return outbox_claim_digest(
            {
                "request_digest": self.request_digest,
                "owner_id": self.owner_id,
                "claim_token": self.claim_token,
                "event_id": self.outbox_event_id,
            }
        )

    def disposition_id(self, subject: RepairSecondarySubject) -> str:
        return str(uuid5(NAMESPACE_URL, self.verification_id + ":" + subject.fingerprint))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "mutation_request_digest": self.mutation_command.request_digest,
            "run_id": self.unit.run_id,
            "unit_id": self.unit.unit_id,
            "generation": self.unit.generation,
            "sequence": self.unit.sequence,
            "attempt": self.unit.attempt,
            "boundary_digest": self.unit.boundary_digest,
            "inventory_binding_digest": build_inventory_binding_digest(self.inventory),
            "owner_id": self.owner_id,
            "claim_token": self.claim_token,
            "verification_contract_version": self.verification_contract_version,
        }


@dataclass(frozen=True)
class RepairSecondaryDispositionDetail:
    subject: RepairSecondarySubject
    action: RepairSecondaryAction
    outcome: RepairSecondaryOutcome
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS or self.outcome == "pending":
            raise ValueError("committed verification disposition is invalid")

    def record(self, command: RepairVerificationCommand) -> RepairSecondaryDisposition:
        payload = disposition_digest(
            {"subject": self.subject.fingerprint, "action": self.action, "outcome": self.outcome}
        )
        return RepairSecondaryDisposition(
            command.unit.run_id,
            command.unit.unit_id,
            command.disposition_id(self.subject),
            command.unit.generation,
            command.unit.sequence,
            command.unit.attempt,
            command.owner_id,
            command.claim_token,
            command.unit.boundary_digest,
            self.subject.fingerprint,
            self.evidence_digest,
            payload,
            self.outcome,
        )


@dataclass(frozen=True)
class RepairAtomicVerificationResult:
    decision: RepairVerificationDecision
    verification: RepairVerificationResult | None
    dispositions: tuple[RepairSecondaryDisposition, ...]
    outbox: RepairOutboxEvent | None
    unit_equation: RepairUnitEquation | None
    derived_state_digest: str | None

    def __post_init__(self) -> None:
        committed = self.decision == "committed"
        replayed = self.decision == "replayed"
        if committed and (
            self.verification is None
            or self.outbox is None
            or self.unit_equation is None
            or self.derived_state_digest is None
        ):
            raise ValueError("committed verification result is incomplete")
        if replayed and (
            self.verification is None
            or self.outbox is None
            or self.unit_equation is None
            or self.derived_state_digest is None
        ):
            raise ValueError("replayed verification result is incoherent")
        if self.decision in {"drift_conflict", "rejected"} and any(
            value is not None
            for value in (
                self.verification,
                self.outbox,
                self.unit_equation,
                self.derived_state_digest,
            )
        ):
            raise ValueError("non-committed verification must not manufacture ledger evidence")
        if self.decision in {"drift_conflict", "rejected"} and self.dispositions:
            raise ValueError("non-committed verification must not manufacture dispositions")
        if committed:
            if (
                self.unit_equation is None
                or not self.dispositions
                or not self.unit_equation.balanced
            ):
                raise ValueError("committed verification result is incoherent")
        if replayed and (
            not self.dispositions
            or self.unit_equation is None
            or not self.unit_equation.balanced
            or self.unit_equation.first_commit_attempt_count != 0
            or self.unit_equation.replay_no_op_count != 1
        ):
            raise ValueError("replayed verification result is incoherent")
