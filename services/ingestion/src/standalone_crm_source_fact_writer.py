"""Non-registered orchestration for standalone CRM source-fact pages."""

from __future__ import annotations

from typing import Protocol

from src.standalone_crm_source_fact_mapper import map_source_fact_page
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactMutation,
    StandaloneCrmSourceFactPage,
    build_source_fact_commit,
)
from src.standalone_crm_unit_repository import StandaloneCrmAtomicUnitCommit


class StandaloneCrmSourceFactCommitter(Protocol):
    """Narrow owned boundary preventing runtime/task wiring."""

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation],
    ) -> StandaloneCrmSourceFactCommitResult:
        """Commit one already-authorized page."""


class StandaloneCrmSourceFactWriter:
    """Map then submit one fenced atomic page commit without source calls."""

    def __init__(self, repository: StandaloneCrmSourceFactCommitter) -> None:
        self._repository = repository

    def write(self, page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactCommitResult:
        """Map once; the repository recalculates duplicate skips under its transaction lock."""
        mutation = map_source_fact_page(page)
        return self._repository.commit_unit(build_source_fact_commit(mutation, skipped_rows=0))
