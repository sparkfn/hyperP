"""A-S1 compile surface limited to source-child authority and atomic commits."""

from __future__ import annotations

from dataclasses import dataclass

from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import ContactSourceChildEnvelope
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmAtomicUnitRepository,
    StandaloneCrmUnitAccountingDelta,
)


@dataclass(frozen=True)
class SourceFactMutation:
    source_record_id: str


@dataclass(frozen=True)
class SourceFactResult:
    committed: bool


class SourceFactRepository(StandaloneCrmAtomicUnitRepository[SourceFactMutation, SourceFactResult]):
    """Test-only A-S1 surface; it is not a concrete domain persistence writer."""

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[SourceFactMutation],
    ) -> SourceFactResult:
        return SourceFactResult(request.mutation.source_record_id != "")


def commit_source_fact(
    envelope: ContactSourceChildEnvelope,
    expected: StandaloneCrmCheckpoint,
    proposed: StandaloneCrmCheckpoint,
) -> bool:
    """Prove A-S1 can consume only its source fact and shared atomic contracts."""
    request = StandaloneCrmAtomicUnitCommit(
        envelope,
        SourceFactMutation("contact-record-a"),
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )
    return SourceFactRepository().commit_unit(request).committed
