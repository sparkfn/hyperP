"""Strict immutable future execution-ledger record values for issues #309--#313."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RepairUnitState = Literal[
    "allocated",
    "quiesced",
    "applied",
    "review_required",
    "failed",
    "rolled_back",
]
RepairCheckpointState = Literal["written", "superseded", "consumed"]
RepairFenceState = Literal["claimed", "released", "lost"]
RepairMutationOutcome = Literal[
    "applied",
    "review_required",
    "no_op",
    "drifted",
    "failed",
]
RepairRollbackState = Literal["available", "restored", "review_required"]
RepairSecondaryOutcome = Literal["pending", "reconciled", "review_required", "failed"]
RepairVerificationOutcome = Literal["pending", "verified", "drifted", "failed"]
RepairOutboxState = Literal["pending", "published", "acknowledged", "failed"]
RepairQuiescenceState = Literal["requested", "quiesced", "released", "lost"]


@dataclass(frozen=True)
class RepairUnit:
    """An immutable allocated inventory unit, never an authorization to execute."""

    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    boundary_digest: str
    inventory_fingerprint: str
    state: RepairUnitState
    inventory_key: str | None = None
    source_record_pk: str | None = None
    inventory_graph_fingerprint: str | None = None
    inventory_stored_payload_fingerprint: str | None = None
    inventory_binding_digest: str | None = None

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _positive(self.generation, "unit generation")
        _nonnegative(self.sequence, "unit sequence")
        _positive(self.attempt, "unit allocation attempt")
        _digest(self.boundary_digest, "unit boundary digest")
        _digest(self.inventory_fingerprint, "unit inventory fingerprint")
        _literal(
            self.state,
            {"allocated", "quiesced", "applied", "review_required", "failed", "rolled_back"},
            "unit state",
        )
        binding = (
            self.inventory_key,
            self.source_record_pk,
            self.inventory_graph_fingerprint,
            self.inventory_stored_payload_fingerprint,
            self.inventory_binding_digest,
        )
        if any(value is not None for value in binding):
            if not all(isinstance(value, str) and value for value in binding):
                raise ValueError("unit inventory binding must be complete")
            assert self.inventory_binding_digest is not None
            _digest(self.inventory_binding_digest, "unit inventory binding digest")


@dataclass(frozen=True)
class RepairCheckpoint:
    """A generation-scoped immutable checkpoint owned by a fence token."""

    run_id: str
    unit_id: str
    checkpoint_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    fence_token: str
    boundary_digest: str
    checkpoint_digest: str
    evidence_digest: str
    state: RepairCheckpointState

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.checkpoint_id, "checkpoint ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "checkpoint")
        _owner_token(self.owner_id, self.fence_token, "checkpoint")
        _digests(
            (self.boundary_digest, "checkpoint boundary digest"),
            (self.checkpoint_digest, "checkpoint digest"),
            (self.evidence_digest, "checkpoint evidence digest"),
        )
        _literal(self.state, {"written", "superseded", "consumed"}, "checkpoint state")


@dataclass(frozen=True)
class RepairFence:
    """A generation- and attempt-specific ownership fence."""

    run_id: str
    unit_id: str
    fence_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    token: str
    boundary_digest: str
    fence_fingerprint: str
    state: RepairFenceState

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.fence_id, "fence ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "fence")
        _owner_token(self.owner_id, self.token, "fence")
        _digests(
            (self.boundary_digest, "fence boundary digest"),
            (self.fence_fingerprint, "fence fingerprint"),
        )
        _literal(self.state, {"claimed", "released", "lost"}, "fence state")


@dataclass(frozen=True)
class RepairMutationResult:
    """The immutable result of one guarded mutation attempt."""

    run_id: str
    unit_id: str
    mutation_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    fence_token: str
    boundary_digest: str
    unit_fingerprint: str
    result_digest: str
    rollback_image_digest: str
    evidence_digest: str
    payload_digest: str
    outcome: RepairMutationOutcome

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.mutation_id, "mutation ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "mutation")
        _owner_token(self.owner_id, self.fence_token, "mutation")
        _digests(
            (self.boundary_digest, "mutation boundary digest"),
            (self.unit_fingerprint, "mutation unit fingerprint"),
            (self.result_digest, "mutation result digest"),
            (self.rollback_image_digest, "mutation rollback image digest"),
            (self.evidence_digest, "mutation evidence digest"),
            (self.payload_digest, "mutation payload digest"),
        )
        _literal(
            self.outcome,
            {"applied", "review_required", "no_op", "drifted", "failed"},
            "mutation outcome",
        )


@dataclass(frozen=True)
class RepairRollbackImage:
    """An immutable rollback image tied to one guarded unit attempt."""

    run_id: str
    unit_id: str
    rollback_image_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    fence_token: str
    boundary_digest: str
    source_fingerprint: str
    image_digest: str
    expected_repaired_digest: str
    evidence_digest: str
    payload_digest: str
    state: RepairRollbackState

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.rollback_image_id, "rollback image ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "rollback image")
        _owner_token(self.owner_id, self.fence_token, "rollback image")
        _digests(
            (self.boundary_digest, "rollback boundary digest"),
            (self.source_fingerprint, "rollback source fingerprint"),
            (self.image_digest, "rollback image digest"),
            (self.expected_repaired_digest, "rollback expected state digest"),
            (self.evidence_digest, "rollback evidence digest"),
            (self.payload_digest, "rollback payload digest"),
        )
        _literal(
            self.state,
            {"available", "restored", "review_required"},
            "rollback state",
        )


@dataclass(frozen=True)
class RepairSecondaryDisposition:
    """A sequenced secondary reconciliation disposition."""

    run_id: str
    unit_id: str
    disposition_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    control_token: str
    boundary_digest: str
    subject_fingerprint: str
    evidence_digest: str
    payload_digest: str
    outcome: RepairSecondaryOutcome

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.disposition_id, "secondary disposition ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "secondary")
        _owner_token(self.owner_id, self.control_token, "secondary")
        _digests(
            (self.boundary_digest, "secondary boundary digest"),
            (self.subject_fingerprint, "secondary subject fingerprint"),
            (self.evidence_digest, "secondary evidence digest"),
            (self.payload_digest, "secondary payload digest"),
        )
        _literal(
            self.outcome,
            {"pending", "reconciled", "review_required", "failed"},
            "secondary outcome",
        )


@dataclass(frozen=True)
class RepairVerificationResult:
    """A sequenced verification or reconciliation result."""

    run_id: str
    unit_id: str
    verification_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    fence_token: str
    boundary_digest: str
    subject_fingerprint: str
    verification_digest: str
    evidence_digest: str
    payload_digest: str
    outcome: RepairVerificationOutcome

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.verification_id, "verification ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "verification")
        _owner_token(self.owner_id, self.fence_token, "verification")
        _digests(
            (self.boundary_digest, "verification boundary digest"),
            (self.subject_fingerprint, "verification subject fingerprint"),
            (self.verification_digest, "verification digest"),
            (self.evidence_digest, "verification evidence digest"),
            (self.payload_digest, "verification payload digest"),
        )
        _literal(
            self.outcome,
            {"pending", "verified", "drifted", "failed"},
            "verification outcome",
        )


@dataclass(frozen=True)
class RepairOutboxEvent:
    """An immutable, sequenced integration event with no dispatch behavior."""

    run_id: str
    unit_id: str
    event_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    delivery_token: str
    boundary_digest: str
    payload_digest: str
    evidence_digest: str
    state: RepairOutboxState

    def __post_init__(self) -> None:
        _scope(self.run_id, self.unit_id)
        _identity(self.event_id, "outbox event ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "outbox")
        _owner_token(self.owner_id, self.delivery_token, "outbox")
        _digests(
            (self.boundary_digest, "outbox boundary digest"),
            (self.payload_digest, "outbox payload digest"),
            (self.evidence_digest, "outbox evidence digest"),
        )
        _literal(
            self.state,
            {"pending", "published", "acknowledged", "failed"},
            "outbox state",
        )


@dataclass(frozen=True)
class RepairQuiescence:
    """A non-executable proof that dispatch was quiesced for one boundary."""

    run_id: str
    quiescence_id: str
    generation: int
    sequence: int
    attempt: int
    owner_id: str
    control_token: str
    boundary_digest: str
    control_fingerprint: str
    evidence_digest: str
    state: RepairQuiescenceState

    def __post_init__(self) -> None:
        _identity(self.run_id, "quiescence run ID")
        _identity(self.quiescence_id, "quiescence ID")
        _generation_sequence_attempt(self.generation, self.sequence, self.attempt, "quiescence")
        _owner_token(self.owner_id, self.control_token, "quiescence")
        _digests(
            (self.boundary_digest, "quiescence boundary digest"),
            (self.control_fingerprint, "quiescence control fingerprint"),
            (self.evidence_digest, "quiescence evidence digest"),
        )
        _literal(
            self.state,
            {"requested", "quiesced", "released", "lost"},
            "quiescence state",
        )


def _scope(run_id: str, unit_id: str) -> None:
    _identity(run_id, "future ledger run ID")
    _identity(unit_id, "future ledger unit ID")


def _generation_sequence_attempt(
    generation: int,
    sequence: int,
    attempt: int,
    record_kind: str,
) -> None:
    _positive(generation, f"{record_kind} generation")
    _nonnegative(sequence, f"{record_kind} sequence")
    _positive(attempt, f"{record_kind} attempt")


def _owner_token(owner_id: str, token: str, record_kind: str) -> None:
    _identity(owner_id, f"{record_kind} owner")
    _identity(token, f"{record_kind} token", maximum=256)


def _digests(*values: tuple[str, str]) -> None:
    for value, label in values:
        _digest(value, label)


def _identity(value: str, label: str, *, maximum: int = 128) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"repair {label} must be non-empty")
    if len(value) > maximum or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"repair {label} is invalid")


def _positive(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"repair {label} must be positive")


def _nonnegative(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"repair {label} must be non-negative")


def _digest(value: str, label: str) -> None:
    raw = value.removeprefix("sha256:")
    if not value.startswith("sha256:") or len(raw) != 64:
        raise ValueError(f"repair {label} must be a lowercase SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in raw):
        raise ValueError(f"repair {label} must be a lowercase SHA-256 digest")


def _literal(value: str, allowed: set[str], label: str) -> None:
    if value not in allowed:
        raise ValueError(f"repair {label} is invalid")
