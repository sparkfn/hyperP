"""Merge repository protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, TypedDict


class GoldenProfileSelection(TypedDict):
    field_name: Literal[
        "preferred_full_name",
        "preferred_dob",
        "preferred_phone",
        "preferred_email",
        "preferred_address",
        "preferred_nric",
    ]
    source_kind: Literal["source_record_fact", "identifier", "address", "literal"]
    selected_value: str
    source_record_pk: str | None
    identifier_type: str | None


@dataclass
class MergeOutcome:
    blocked: bool = False
    not_found: bool = False
    merge_event_id: str | None = None
    redirected_review_case_ids: list[str] = field(default_factory=list)


class MergeRepository(Protocol):
    async def manual_merge(
        self,
        from_id: str,
        to_id: str,
        reason: str,
        actor_id: str,
        golden_profile_selections: list[GoldenProfileSelection],
    ) -> MergeOutcome: ...

    async def unmerge(
        self, merge_event_id: str, reason: str, actor_id: str
    ) -> tuple[str, str] | None:
        """Returns (absorbed_id, survivor_id) or None if not found."""
        ...

    async def create_lock(
        self,
        left: str,
        right: str,
        lock_type: str,
        reason: str,
        expires_at: str | None,
        actor_id: str,
    ) -> tuple[str, str | None]:
        """Returns (status, lock_id). Status is 'ok', 'conflict', or 'not_found'."""
        ...

    async def delete_lock(self, lock_id: str) -> bool: ...
