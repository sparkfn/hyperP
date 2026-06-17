"""Review repository protocol."""

from __future__ import annotations

from typing import Protocol, TypedDict

from src.repositories.protocols.merge import GoldenProfileSelection
from src.types import ReviewCaseDetail, ReviewCaseSummary


class ReviewListFilters(TypedDict, total=False):
    queue_state: str | None
    assigned_to: str | None
    person_id: str | None
    priority_lte: int | None
    priority_gte: int | None
    decision: str | None
    engine_type: str | None
    confidence_gte: float | None
    confidence_lte: float | None
    created_after: str | None
    created_before: str | None
    sla_due_after: str | None
    sla_due_before: str | None
    overdue_sla: bool | None
    resolved: bool | None
    q: str | None
    sort_by: str | None
    sort_order: str | None


class AssignResult(TypedDict):
    review_case_id: str
    queue_state: str
    assigned_to: str


class ActionResult(TypedDict, total=False):
    review_case_id: str
    queue_state: str
    resolution: str | None
    survivor_person_id: str | None
    golden_profile_selections: list[GoldenProfileSelection]
    merge_blocked: bool
    merge_not_applicable: bool


class ReviewRepository(Protocol):
    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], int]:
        """Returns (items, total). Route computes has_more = skip + limit < total."""
        ...

    async def get_by_id(self, review_case_id: str) -> ReviewCaseDetail | None: ...

    async def get_by_match_decision_id(self, match_decision_id: str) -> ReviewCaseDetail | None: ...

    async def recreate(self, review_case_id: str, actor_id: str) -> ReviewCaseDetail | None: ...

    async def assign(self, review_case_id: str, assigned_to: str) -> AssignResult | None: ...

    async def submit_action(
        self,
        review_case_id: str,
        action_type: str,
        new_state: str,
        resolution: str | None,
        notes: str | None,
        follow_up_at: str | None,
        actor_id: str,
        survivor_person_id: str | None,
        golden_profile_selections: list[GoldenProfileSelection],
    ) -> ActionResult | None: ...
