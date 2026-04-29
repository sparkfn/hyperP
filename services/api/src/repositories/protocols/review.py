"""Review repository protocol."""

from __future__ import annotations

from typing import Protocol, TypedDict

from src.types import ReviewCaseDetail, ReviewCaseSummary


class ReviewListFilters(TypedDict, total=False):
    queue_state: str | None
    assigned_to: str | None
    priority_lte: int | None


class AssignResult(TypedDict):
    review_case_id: str
    queue_state: str
    assigned_to: str


class ActionResult(TypedDict, total=False):
    review_case_id: str
    queue_state: str
    resolution: str | None
    survivor_person_id: str | None
    merge_blocked: bool
    merge_not_applicable: bool


class ReviewRepository(Protocol):
    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], bool]:
        """Returns (items, has_more). has_more detected via +1 fetch."""
        ...

    async def get_by_id(self, review_case_id: str) -> ReviewCaseDetail | None: ...

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
    ) -> ActionResult | None: ...
