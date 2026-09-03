"""Strict command, result, status, and bounded-drift contracts for #312 rollback."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from src.crm_deal_identity_repair.digests import (
    rollback_authority_digest,
    rollback_drift_digest,
    rollback_request_digest,
    rollback_result_digest,
    rollback_status_digest,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairFence,
    RepairMutationResult,
    RepairRollbackImage,
    RepairSecondaryDisposition,
    RepairUnit,
)
from src.models import JsonValue

RollbackDecision = Literal[
    "restored",
    "reviewed_compensation_required",
    "replayed",
    "rejected",
]
RollbackFailureStage = Literal[
    "after_guard",
    "after_lock",
    "after_compare",
    "after_restore",
    "after_postcondition",
    "after_ledger",
]


@dataclass(frozen=True)
class RepairRollbackAuthorization:
    """The single-use fenced transition that can consume one mutation image."""

    unit: RepairUnit
    fence: RepairFence
    mutation: RepairMutationResult
    image: RepairRollbackImage
    authorization_reference: str
    authorization_token_digest: str
    predecessor_transition_id: str
    authorization_policy: str
    authorization_transition_id: str

    def __post_init__(self) -> None:
        if any(
            not value or len(value) > 200
            for value in (
                self.authorization_reference,
                self.authorization_token_digest,
                self.predecessor_transition_id,
                self.authorization_policy,
                self.authorization_transition_id,
            )
        ):
            raise ValueError("rollback authorization transition is invalid")
        _validate_authorization_token_digest(self.authorization_token_digest)
        records = (self.unit, self.fence, self.mutation, self.image)
        if any(
            item.run_id != self.unit.run_id or item.unit_id != self.unit.unit_id for item in records
        ):
            raise ValueError("rollback authority scope differs")
        if any(
            (item.generation, item.sequence, item.attempt, item.boundary_digest)
            != (
                self.unit.generation,
                self.unit.sequence,
                self.unit.attempt,
                self.unit.boundary_digest,
            )
            for item in records
        ):
            raise ValueError("rollback authority generation differs")
        if self.fence.state != "claimed" or self.unit.state not in {"applied", "review_required"}:
            raise ValueError("rollback authority is not currently consumable")
        if not isinstance(self.unit.source_record_pk, str) or not self.unit.source_record_pk:
            raise ValueError("rollback authority lacks original source binding")
        if self.mutation.mutation_id == "" or self.image.state not in {
            "available",
            "restored",
            "review_required",
        }:
            raise ValueError("rollback authority has invalid immutable identities")
        if self.mutation.rollback_image_digest != self.image.image_digest:
            raise ValueError("rollback authority image digest differs")
        if (
            self.mutation.fence_token != self.fence.token
            or self.image.fence_token != self.fence.token
            or self.mutation.owner_id != self.fence.owner_id
            or self.image.owner_id != self.fence.owner_id
        ):
            raise ValueError("rollback authority fence differs")
        if self.mutation.unit_fingerprint != self.unit.inventory_fingerprint:
            raise ValueError("rollback authority unit fingerprint differs")
        if (
            self.mutation.evidence_digest != self.image.evidence_digest
            or self.mutation.payload_digest != self.image.payload_digest
        ):
            raise ValueError("rollback authority immutable evidence differs")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.unit.run_id,
            "unit_id": self.unit.unit_id,
            "generation": self.unit.generation,
            "sequence": self.unit.sequence,
            "attempt": self.unit.attempt,
            "owner_id": self.fence.owner_id,
            "fence_id": self.fence.fence_id,
            "fence_token": self.fence.token,
            "boundary_digest": self.unit.boundary_digest,
            "mutation_id": self.mutation.mutation_id,
            "rollback_image_id": self.image.rollback_image_id,
            "image_digest": self.image.image_digest,
            "expected_repaired_digest": self.image.expected_repaired_digest,
            "authorization_reference": self.authorization_reference,
            "authorization_token_digest": self.authorization_token_digest,
            "predecessor_transition_id": self.predecessor_transition_id,
            "authorization_policy": self.authorization_policy,
            "authorization_transition_id": self.authorization_transition_id,
        }

    @property
    def digest(self) -> str:
        return rollback_authority_digest(self.to_dict())


@dataclass(frozen=True)
class RepairRollbackCommand:
    """A deterministic, non-allocating request to restore one mutation image."""

    authorization: RepairRollbackAuthorization
    rollback_contract_version: str = "crm_deal_identity_repair_rollback_v1"

    def __post_init__(self) -> None:
        if self.rollback_contract_version != "crm_deal_identity_repair_rollback_v1":
            raise ValueError("rollback contract version is invalid")

    @property
    def disposition_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.request_digest + ":disposition"))

    @property
    def request_digest(self) -> str:
        return rollback_request_digest(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "contract_version": self.rollback_contract_version,
            "authorization": self.authorization.to_dict(),
        }


@dataclass(frozen=True)
class RepairRollbackDrift:
    """Bounded, non-sensitive mismatch evidence persisted only with valid authority."""

    total_mismatch_count: int
    summaries: tuple[tuple[str, str], ...]
    complete_digest: str

    def __post_init__(self) -> None:
        if self.total_mismatch_count < 1 or len(self.summaries) > 20:
            raise ValueError("rollback drift evidence bounds are invalid")
        if any(not identity or not reason for identity, reason in self.summaries):
            raise ValueError("rollback drift summary is invalid")
        if not self.complete_digest.startswith("sha256:"):
            raise ValueError("rollback drift digest is invalid")

    @classmethod
    def from_rows(cls, rows: tuple[tuple[str, str], ...]) -> RepairRollbackDrift:
        ordered = tuple(sorted(set(rows)))
        payload: dict[str, JsonValue] = {
            "mismatches": [{"identity": identity, "reason": reason} for identity, reason in ordered]
        }
        return cls(len(ordered), ordered[:20], rollback_drift_digest(payload))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "total_mismatch_count": self.total_mismatch_count,
            "summaries": [
                {"identity": identity, "reason": reason} for identity, reason in self.summaries
            ],
            "complete_digest": self.complete_digest,
        }


@dataclass(frozen=True)
class RepairRollbackResult:
    """One terminal rollback outcome; rejected authority has no disposition."""

    decision: RollbackDecision
    image_state: Literal["available", "restored", "review_required"]
    result_digest: str
    disposition: RepairSecondaryDisposition | None = None
    drift: RepairRollbackDrift | None = None
    original_terminal_decision: Literal["restored", "reviewed_compensation_required"] | None = None

    def __post_init__(self) -> None:
        if self.decision == "restored" and (
            self.image_state != "restored" or self.disposition is None
        ):
            raise ValueError("restored rollback result is incomplete")
        if self.decision == "reviewed_compensation_required" and (
            self.image_state != "review_required" or self.disposition is None or self.drift is None
        ):
            raise ValueError("drift rollback result is incomplete")
        if self.decision == "rejected" and (self.disposition is not None or self.drift is not None):
            raise ValueError("rejected rollback must not write evidence")
        if self.decision == "replayed":
            if self.disposition is None or self.original_terminal_decision is None:
                raise ValueError("replayed rollback result lacks original terminal decision")
            if self.original_terminal_decision == "restored" and self.drift is not None:
                raise ValueError("restored replay contains drift")
            if (
                self.original_terminal_decision == "reviewed_compensation_required"
                and self.drift is None
            ):
                raise ValueError("compensation replay lacks drift")


@dataclass(frozen=True)
class RepairRollbackStatus:
    """Read-only validated state for an immutable image and optional disposition."""

    image_state: Literal["available", "restored", "review_required"]
    terminal_disposition_id: str | None
    status_digest: str

    @classmethod
    def create(
        cls,
        image_state: Literal["available", "restored", "review_required"],
        terminal_id: str | None,
        status_evidence: dict[str, JsonValue] | None = None,
    ) -> RepairRollbackStatus:
        evidence: dict[str, JsonValue] = {
            "image_state": image_state,
            "terminal_disposition_id": terminal_id,
        }
        if status_evidence is not None:
            evidence["bundle"] = status_evidence
        return cls(
            image_state,
            terminal_id,
            rollback_status_digest(evidence),
        )


def build_rollback_result_digest(
    command: RepairRollbackCommand,
    decision: RollbackDecision,
    image_state: str,
    drift: RepairRollbackDrift | None = None,
) -> str:
    """Build the immutable result digest without serializing sensitive graph content."""
    payload: dict[str, JsonValue] = {
        "request_digest": command.request_digest,
        "decision": decision,
        "image_state": image_state,
    }
    if drift is not None:
        payload["drift"] = drift.to_dict()
    return rollback_result_digest(payload)


def build_rollback_status_digest(
    command: RepairRollbackCommand,
    image_state: Literal["available", "restored", "review_required"],
    terminal_disposition_id: str | None,
    terminal_decision: Literal["restored", "reviewed_compensation_required"] | None,
    result_digest: str | None,
    drift: RepairRollbackDrift | None,
) -> str:
    """Digest the complete immutable terminal identity used by status and replay."""
    payload: dict[str, JsonValue] = {
        "request_digest": command.request_digest,
        "authorization_digest": command.authorization.digest,
        "image_state": image_state,
        "terminal_disposition_id": terminal_disposition_id,
        "terminal_decision": terminal_decision,
        "result_digest": result_digest,
    }
    if drift is not None:
        payload["drift"] = drift.to_dict()
    return rollback_status_digest(payload)


def _validate_authorization_token_digest(value: str) -> None:
    raw = value.removeprefix("sha256:")
    if (
        not value.startswith("sha256:")
        or len(raw) != 64
        or any(character not in "0123456789abcdef" for character in raw)
    ):
        raise ValueError("rollback authorization token digest is invalid")
