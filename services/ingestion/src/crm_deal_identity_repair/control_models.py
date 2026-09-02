"""Strict, non-executable control-plane values for issue #310.

The qualification run remains immutable and ``qualified``.  These records are a
separate, revisioned control overlay; none represents permission to mutate CRM
data or dispatch a task.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import _digest, _identity, _nonnegative

RepairControlState = Literal["qualified", "quiescing", "quiesced", "allocated", "paused", "lost"]

_CONTROL_TOKEN_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
RepairPublicationState = Literal["preparing", "publishing", "confirmed"]


@dataclass(frozen=True)
class CapturedTaskTopologyIdentity:
    """One task identity from the topology frozen before absence inspection.

    ``run_id`` identifies the repair-control CAS and is deliberately absent
    here: Celery deliveries identify a Bitrix generation, logical run, and
    attempt instead.
    """

    control_instance_id: str
    generation_id: str
    logical_run_id: str
    attempt_generation: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.control_instance_id, "task control instance"),
            (self.generation_id, "task generation"),
            (self.logical_run_id, "task logical run"),
        ):
            _identity(value, label)
            if ";" in value or "=" in value:
                raise ValueError(f"repair {label} contains a selector delimiter")
        _nonnegative(self.attempt_generation, "task attempt generation")

    def selector(self) -> str:
        return (
            f"control_instance_id={self.control_instance_id};"
            f"generation_id={self.generation_id};"
            f"logical_run_id={self.logical_run_id};"
            f"attempt_generation={self.attempt_generation}"
        )


@dataclass(frozen=True)
class RepairControlRequest:
    """One compare-and-set command identity supplied by an operator."""

    repair_id: str
    run_id: str
    owner_id: str
    token: str
    expected_revision: int

    def __post_init__(self) -> None:
        for value, label in ((self.repair_id, "repair ID"), (self.run_id, "run ID")):
            _identity(value, label)
        _identity(self.owner_id, "control owner")
        _identity(self.token, "control token", maximum=256)
        object.__setattr__(self, "token", control_token_digest(self.token))
        _nonnegative(self.expected_revision, "control expected revision")


def control_token_digest(token: str) -> str:
    """Return the domain-separated durable representation of an operator secret."""
    if _CONTROL_TOKEN_DIGEST.fullmatch(token):
        return token
    return object_digest(b"crm-deal-identity-repair-control-token-v1\x00", {"token": token})


@dataclass(frozen=True)
class RepairDispatchLease:
    """The exact Bitrix dispatch ownership record returned by a CAS."""

    control_instance_id: str
    run_id: str
    owner_id: str
    token: str
    revision: int
    state: RepairControlState
    boundary_digest: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.control_instance_id, "control instance"),
            (self.run_id, "run ID"),
            (self.owner_id, "control owner"),
        ):
            _identity(value, label)
        _identity(self.token, "control token", maximum=256)
        _nonnegative(self.revision, "dispatch revision")
        _digest(self.boundary_digest, "dispatch boundary digest")
        if self.state not in {"qualified", "quiescing", "quiesced", "allocated", "paused", "lost"}:
            raise ValueError("repair dispatch state is invalid")


@dataclass(frozen=True)
class RepairPublicationReservation:
    """A durable, fail-closed reservation made before source-window freezing."""

    reservation_id: str
    control_instance_id: str
    publication_key: str
    state: RepairPublicationState
    revision: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.reservation_id, "publication reservation ID"),
            (self.control_instance_id, "control instance"),
            (self.publication_key, "publication key"),
        ):
            _identity(value, label)
        _nonnegative(self.revision, "publication revision")
        if self.state not in {"preparing", "publishing", "confirmed"}:
            raise ValueError("repair publication reservation state is invalid")


@dataclass(frozen=True)
class RepairAllocationCompletion:
    """One immutable allocation completion; zero units is a valid completion."""

    run_id: str
    completion_id: str
    boundary_digest: str
    overlay_digest: str
    allocation_digest: str
    unit_count: int

    def __post_init__(self) -> None:
        _identity(self.run_id, "allocation run ID")
        _identity(self.completion_id, "allocation completion ID")
        _digest(self.boundary_digest, "allocation boundary digest")
        _digest(self.overlay_digest, "allocation overlay digest")
        _digest(self.allocation_digest, "allocation digest")
        _nonnegative(self.unit_count, "allocation unit count")


@dataclass(frozen=True)
class RepairControlStatus:
    """Read-only combined #300 qualification and #310 control-plane status."""

    repair_id: str
    qualification_status: Literal["qualified", "not_qualified"]
    control_state: RepairControlState | None
    dispatch_blocked: bool | None
    dispatch_revision: int | None
    quiescence_state: Literal["not_quiesced", "quiesced"] | None
    allocation_state: Literal["not_allocated", "allocated"] | None
    paused_from_state: Literal["quiesced", "allocated"] | None
    allocated_unit_count: int | None
    execution_allowed: Literal[False] = False

    def __post_init__(self) -> None:
        _identity(self.repair_id, "repair ID")
        if self.execution_allowed is not False:
            raise ValueError("repair control status must remain non-executable")
        if self.qualification_status == "not_qualified":
            if any(
                value is not None
                for value in (
                    self.control_state,
                    self.dispatch_blocked,
                    self.dispatch_revision,
                    self.quiescence_state,
                    self.allocation_state,
                    self.paused_from_state,
                    self.allocated_unit_count,
                )
            ):
                raise ValueError("unqualified repair cannot have control state")
            return
        if self.control_state is not None and self.control_state not in {
            "qualified",
            "quiescing",
            "quiesced",
            "allocated",
            "paused",
            "lost",
        }:
            raise ValueError("repair control status state is invalid")
        if self.dispatch_blocked is not None and not isinstance(self.dispatch_blocked, bool):
            raise ValueError("repair dispatch blocked state is invalid")
        if self.dispatch_revision is not None:
            _nonnegative(self.dispatch_revision, "status dispatch revision")
        if self.quiescence_state not in {None, "not_quiesced", "quiesced"}:
            raise ValueError("repair quiescence state is invalid")
        if self.allocation_state not in {None, "not_allocated", "allocated"}:
            raise ValueError("repair allocation state is invalid")
        if self.paused_from_state not in {None, "quiesced", "allocated"}:
            raise ValueError("repair pause state is invalid")
        if self.allocated_unit_count is not None:
            _nonnegative(self.allocated_unit_count, "status allocation count")
