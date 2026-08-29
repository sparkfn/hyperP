from __future__ import annotations

from dataclasses import dataclass

from src.connectors.bitrix_openlines.models import CrmContact
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactPage,
)
from src.standalone_crm_source_fact_writer import StandaloneCrmSourceFactWriter
from src.standalone_crm_unit_repository import StandaloneCrmAtomicUnitCommit
from tests._standalone_crm_lane_a_fakes import lead_envelope


@dataclass
class _Repository:
    calls: int = 0
    request: object | None = None

    def commit_unit(
        self, request: StandaloneCrmAtomicUnitCommit[object]
    ) -> StandaloneCrmSourceFactCommitResult:
        self.calls += 1
        self.request = request
        return StandaloneCrmSourceFactCommitResult("committed", 1, 0, 0)


def test_writer_maps_only_builds_typed_atomic_request_and_performs_no_source_call() -> None:
    envelope = lead_envelope()
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, None, None, 0, 0, 1, 2)
    page = StandaloneCrmSourceFactPage(
        envelope, "call-a", 5, checkpoint, (CrmContact("6", "Ada", kind="lead"),)
    )
    repository = _Repository()

    result = StandaloneCrmSourceFactWriter(repository).write(page)

    assert result.committed and repository.calls == 1
    assert isinstance(repository.request, StandaloneCrmAtomicUnitCommit)
    assert repository.request.proposed_checkpoint.last_committed_id == 6
    assert repository.request.accounting_delta.processed_rows == 1
