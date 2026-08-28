"""Generic atomic source-unit commit protocol for future component writers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar

from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_types import _integer
from src.standalone_crm_child_contracts import (
    CompanySourceChildEnvelope,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
    StandaloneCrmSourceChildEnvelope,
)

MutationT = TypeVar("MutationT")
ResultT = TypeVar("ResultT")


@dataclass(frozen=True)
class StandaloneCrmUnitAccountingDelta:
    """One component-owned accounting delta for a fenced source-unit commit."""

    processed_rows: int
    skipped_rows: int
    failed_rows: int

    def __post_init__(self) -> None:
        for field in ("processed_rows", "skipped_rows", "failed_rows"):
            _integer(getattr(self, field), field)
        if self.skipped_rows > self.processed_rows:
            raise ValueError("skipped_rows cannot exceed processed_rows")


@dataclass(frozen=True)
class StandaloneCrmAtomicUnitCommit[MutationT]:
    """Immutable input to one all-or-nothing component-owned transaction."""

    envelope: StandaloneCrmSourceChildEnvelope
    mutation: MutationT
    expected_checkpoint: StandaloneCrmCheckpoint
    proposed_checkpoint: StandaloneCrmCheckpoint
    accounting_delta: StandaloneCrmUnitAccountingDelta

    def __post_init__(self) -> None:
        envelope = self.envelope
        if not isinstance(
            envelope,
            (ContactSourceChildEnvelope, LeadSourceChildEnvelope, CompanySourceChildEnvelope),
        ):
            raise ValueError("envelope must be one concrete source child authority")
        expected = self.expected_checkpoint
        proposed = self.proposed_checkpoint
        if not isinstance(expected, StandaloneCrmCheckpoint):
            raise ValueError("expected_checkpoint must be a v1 census checkpoint")
        if not isinstance(proposed, StandaloneCrmCheckpoint):
            raise ValueError("proposed_checkpoint must be a v1 census checkpoint")
        if not isinstance(self.accounting_delta, StandaloneCrmUnitAccountingDelta):
            raise ValueError("accounting_delta must be a standalone CRM unit delta")
        if (
            expected.census_id != envelope.unit.census_id
            or proposed.census_id != envelope.unit.census_id
            or expected.stream_kind != envelope.unit.stream_kind
            or proposed.stream_kind != envelope.unit.stream_kind
            or expected.frozen_upper_id != envelope.frozen_upper_id
            or proposed.frozen_upper_id != envelope.frozen_upper_id
            or expected.revision_id is not None
            or proposed.revision_id is not None
            or expected.generation != envelope.unit.generation
            or proposed.generation != envelope.unit.generation
            or expected.fence_token != envelope.unit.fence_token
            or proposed.fence_token != envelope.unit.fence_token
        ):
            raise ValueError("checkpoint does not match source child authority")
        if not expected.can_advance_to(proposed):
            raise ValueError("proposed checkpoint cannot advance expected checkpoint")
        _validate_checkpoint_position(envelope, expected, proposed)
        if (
            proposed.processed_rows - expected.processed_rows
            != self.accounting_delta.processed_rows
        ):
            raise ValueError("processed accounting delta does not match proposed checkpoint")
        if proposed.skipped_rows - expected.skipped_rows != self.accounting_delta.skipped_rows:
            raise ValueError("skipped accounting delta does not match proposed checkpoint")


class StandaloneCrmAtomicUnitRepository[MutationT, ResultT](Protocol):
    """One fenced atomic transaction, implemented independently by each component.

    An implementation must atomically reassert source/control/census/unit,
    generation/fence/task, and budget authority; validate bound, cursor,
    sub-position, deadlines, and row authority; apply its component mutation;
    update processed, skipped, and failed-row accounting; and advance the
    unchanged #273 v1 checkpoint. The whole operation commits or rolls back
    together. This protocol deliberately exposes no callback, raw transaction,
    unfenced head writer, or concrete persistence implementation.
    """

    def commit_unit(self, request: StandaloneCrmAtomicUnitCommit[MutationT]) -> ResultT:
        """Commit the documented domain, accounting, and #273 checkpoint unit or none."""


def _validate_checkpoint_position(
    envelope: StandaloneCrmSourceChildEnvelope,
    expected: StandaloneCrmCheckpoint,
    proposed: StandaloneCrmCheckpoint,
) -> None:
    if expected.last_committed_id != envelope.last_committed_id:
        raise ValueError("expected checkpoint cursor must equal the source child envelope")
    if isinstance(envelope, ContactSourceChildEnvelope):
        _validate_contact_checkpoint_position(envelope, expected)
        return
    if expected.binding_subject_id is not None or expected.binding_offset is not None:
        raise ValueError("lead and company checkpoints cannot carry contact binding position")
    if proposed.binding_subject_id is not None or proposed.binding_offset is not None:
        raise ValueError("lead and company checkpoints cannot carry contact binding position")


def _validate_contact_checkpoint_position(
    envelope: ContactSourceChildEnvelope,
    expected: StandaloneCrmCheckpoint,
) -> None:
    position = envelope.binding_subposition
    if position is None:
        if expected.binding_subject_id is not None or expected.binding_offset is not None:
            raise ValueError(
                "contact checkpoint binding position must equal the source child envelope"
            )
        return
    if (
        expected.binding_subject_id != position.binding_subject_id
        or expected.binding_offset != position.binding_offset
    ):
        raise ValueError("contact checkpoint binding position must equal the source child envelope")
