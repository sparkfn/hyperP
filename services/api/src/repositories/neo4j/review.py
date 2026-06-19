"""Neo4j implementation of ReviewRepository."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from neo4j import AsyncManagedTransaction

from src.celery_client import enqueue_match_recalculation
from src.graph.client import get_session
from src.graph.converters import GraphRecord, to_int, to_optional_str, to_str
from src.graph.golden_profile import recompute_golden_profile_tx
from src.graph.mappers import map_review_case_detail, map_review_case_summary
from src.graph.queries import (
    ASSIGN_REVIEW_CASE,
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_NO_MATCH_LOCK,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    EXECUTE_MANUAL_MERGE,
    GET_PERSONS_FOR_REVIEW_MERGE,
    GET_REVIEW_CASE,
    GET_REVIEW_CASE_BY_MATCH_DECISION,
    LINK_REVIEW_SALES_BOUGHT_UNIT,
    LINK_REVIEW_SALES_PURCHASED_ORDER,
    MARK_REVIEW_SALES_RECORD_LINKED,
    MARK_REVIEW_SALES_RECORD_UNRESOLVED,
    RECREATE_REVIEW_CASE,
    build_count_review_cases_query,
    build_list_review_cases_query,
    build_review_action_cypher,
)
from src.repositories.neo4j._merge_side_effects import apply_merge_review_side_effects
from src.repositories.neo4j.merge import (
    _apply_golden_profile_selections_tx,
    are_valid_golden_profile_selections,
)
from src.repositories.protocols.merge import GoldenProfileSelection
from src.repositories.protocols.review import ActionResult, AssignResult, ReviewListFilters
from src.types import ApiReviewActionType, ReviewCaseDetail, ReviewCaseSummary

from ._utils import record_to_dict, to_total

# ReviewListFilters keys consumed only when building the query string, never
# bound as Cypher parameters.
_NON_CYPHER_KEYS: frozenset[str] = frozenset({"sort_by", "sort_order"})


def _action_entry_json(action_type: str, actor_type: str, actor_id: str, notes: str | None) -> str:
    """Serialize one review-action audit entry; rc.actions stores JSON strings."""
    return json.dumps(
        {
            "action_type": action_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "notes": notes,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )


class Neo4jReviewRepository:
    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], int]:
        has_q = filters.get("q") is not None
        has_person = filters.get("person_id") is not None
        list_query = build_list_review_cases_query(
            filters.get("sort_by"), filters.get("sort_order"), has_q=has_q, has_person=has_person
        )
        count_query = build_count_review_cases_query(has_q=has_q, has_person=has_person)
        cypher_params: dict[str, str | int | float | bool | None] = {
            k: v  # type: ignore[misc]  # TypedDict values are object; known-safe filter keys
            for k, v in filters.items()
            if k not in _NON_CYPHER_KEYS
        }
        list_params = {**cypher_params, "skip": skip, "limit": limit}

        async def _run_list() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(list_query, list_params)
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(count_query, cypher_params)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_list(), _run_count())
        return [map_review_case_summary(rec) for rec in records], total

    async def get_by_id(self, review_case_id: str) -> ReviewCaseDetail | None:
        async with get_session() as session:
            result = await session.run(GET_REVIEW_CASE, review_case_id=review_case_id)
            record = await result.single()
        if record is None:
            return None
        return map_review_case_detail(record_to_dict(record.keys(), list(record.values())))

    async def get_by_match_decision_id(self, match_decision_id: str) -> ReviewCaseDetail | None:
        async with get_session() as session:
            id_result = await session.run(
                GET_REVIEW_CASE_BY_MATCH_DECISION,
                match_decision_id=match_decision_id,
            )
            id_record = await id_result.single()
        if id_record is None:
            return None
        return await self.get_by_id(to_str(id_record["review_case_id"]))

    async def recreate(self, review_case_id: str, actor_id: str) -> ReviewCaseDetail | None:
        action_json = _action_entry_json("recreate", "user", actor_id, None)
        async with get_session(write=True) as session:
            result = await session.run(
                RECREATE_REVIEW_CASE,
                review_case_id=review_case_id,
                action_json=action_json,
            )
            record = await result.single()
        if record is None:
            return None
        return await self.get_by_id(to_str(record["review_case_id"]))

    async def assign(self, review_case_id: str, assigned_to: str) -> AssignResult | None:
        async with get_session(write=True) as session:
            record = await session.execute_write(_assign_tx, review_case_id, assigned_to)
        if record is None:
            return None
        return AssignResult(
            review_case_id=to_str(record.get("review_case_id")),
            queue_state=to_str(record.get("queue_state")),
            assigned_to=to_str(record.get("assigned_to")),
        )

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
    ) -> ActionResult | None:
        if not are_valid_golden_profile_selections(golden_profile_selections):
            return ActionResult(merge_not_applicable=True)
        async with get_session(write=True) as session:
            result = await session.execute_write(
                _action_tx,
                review_case_id,
                action_type,
                new_state,
                resolution,
                notes,
                follow_up_at,
                actor_id,
                survivor_person_id,
                golden_profile_selections,
            )

        if result is None:
            return None

        # Recompute golden profile for the surviving person after a merge
        survivor_id = to_optional_str(result.get("survivor_person_id"))
        selections = result.get("golden_profile_selections", [])
        if action_type == ApiReviewActionType.MERGE.value and survivor_id:
            async with get_session(write=True) as session:
                await session.execute_write(recompute_golden_profile_tx, survivor_id)
                if selections:
                    await session.execute_write(
                        _apply_golden_profile_selections_tx,
                        survivor_id,
                        selections,
                    )

        enqueue_match_recalculation(result.get("redirected_review_case_ids", []))

        return result


async def _assign_tx(
    tx: AsyncManagedTransaction, review_case_id: str, assigned_to: str
) -> GraphRecord | None:
    result = await tx.run(
        ASSIGN_REVIEW_CASE,
        review_case_id=review_case_id,
        assigned_to=assigned_to,
        action_json=_action_entry_json("assign", "system", assigned_to, None),
    )
    record = await result.single()
    if record is None:
        return None
    return dict(record["review_case"])


async def _sales_link_merge_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    new_state: str,
    resolution: str | None,
    follow_up_at: str | None,
    action_type: str,
    actor_id: str,
    notes: str | None,
) -> ActionResult:
    """Approve a sales-record review case: link Order+Units to the candidate Person."""
    await tx.run(LINK_REVIEW_SALES_PURCHASED_ORDER, review_case_id=review_case_id)
    await tx.run(LINK_REVIEW_SALES_BOUGHT_UNIT, review_case_id=review_case_id)
    linked_result = await tx.run(MARK_REVIEW_SALES_RECORD_LINKED, review_case_id=review_case_id)
    if await linked_result.single() is None:
        return ActionResult(merge_not_applicable=True)
    cypher = build_review_action_cypher(resolution, follow_up_at)
    rc_result = await tx.run(
        cypher,
        review_case_id=review_case_id,
        new_state=new_state,
        resolution=resolution,
        follow_up_at=follow_up_at,
        action_json=_action_entry_json(action_type, "reviewer", actor_id, notes),
    )
    rc_record = await rc_result.single()
    if rc_record is None:
        return ActionResult(merge_not_applicable=True)
    rc = dict(rc_record["review_case"])
    return ActionResult(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        resolution=to_optional_str(rc.get("resolution")),
    )


async def _action_tx(
    tx: AsyncManagedTransaction,
    review_case_id: str,
    action_type: str,
    new_state: str,
    resolution: str | None,
    notes: str | None,
    follow_up_at: str | None,
    actor_id: str,
    survivor_person_id: str | None,
    golden_profile_selections: list[GoldenProfileSelection],
) -> ActionResult | None:
    absorbed_id: str | None = None
    survivor_id: str | None = None

    if action_type == ApiReviewActionType.MERGE.value:
        persons_result = await tx.run(GET_PERSONS_FOR_REVIEW_MERGE, review_case_id=review_case_id)
        persons_record = await persons_result.single()
        if persons_record is None:
            return await _sales_link_merge_tx(
                tx,
                review_case_id,
                new_state,
                resolution,
                follow_up_at,
                action_type,
                actor_id,
                notes,
            )

        left_id = to_str(persons_record["left_person_id"])
        right_id = to_str(persons_record["right_person_id"])

        if survivor_person_id == right_id:
            survivor_id, absorbed_id = right_id, left_id
        elif survivor_person_id == left_id:
            survivor_id, absorbed_id = left_id, right_id
        elif survivor_person_id is None:
            # Default to whichever person has more golden fields filled in;
            # fall back to left on a tie.
            left_score = to_int(persons_record["left_completion"])
            right_score = to_int(persons_record["right_completion"])
            if right_score > left_score:
                survivor_id, absorbed_id = right_id, left_id
            else:
                survivor_id, absorbed_id = left_id, right_id
        else:
            return ActionResult(merge_not_applicable=True)

        active_result = await tx.run(
            CHECK_BOTH_PERSONS_ACTIVE, from_id=absorbed_id, to_id=survivor_id
        )
        if await active_result.single() is None:
            return ActionResult(merge_not_applicable=True)

        lock_left, lock_right = (
            (absorbed_id, survivor_id) if absorbed_id < survivor_id else (survivor_id, absorbed_id)
        )
        lock_result = await tx.run(CHECK_NO_MATCH_LOCK, left=lock_left, right=lock_right)
        lock_record = await lock_result.single()
        if lock_record is not None and bool(lock_record["is_locked"]):
            return ActionResult(merge_blocked=True)

    cypher = build_review_action_cypher(resolution, follow_up_at)
    result = await tx.run(
        cypher,
        review_case_id=review_case_id,
        new_state=new_state,
        resolution=resolution,
        follow_up_at=follow_up_at,
        action_json=_action_entry_json(action_type, "reviewer", actor_id, notes),
    )
    record = await result.single()
    if record is None:
        return None

    rc = dict(record["review_case"])
    out = ActionResult(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        resolution=to_optional_str(rc.get("resolution")),
        redirected_review_case_ids=[],
    )

    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value:
        await tx.run(
            CREATE_NO_MATCH_LOCK_FROM_REVIEW,
            review_case_id=review_case_id,
            notes=notes or "Manual no-match from review",
            actor_id=actor_id,
        )
    elif action_type == ApiReviewActionType.MERGE.value and absorbed_id and survivor_id:
        merge_result = await tx.run(
            EXECUTE_MANUAL_MERGE,
            from_id=absorbed_id,
            to_id=survivor_id,
            reason=notes or "Review merge",
            actor_id=actor_id,
        )
        merge_record = await merge_result.single()
        merge_event_id = to_str(merge_record["merge_event_id"]) if merge_record else ""
        if merge_event_id:
            redirected_ids = await apply_merge_review_side_effects(
                tx, merge_event_id, absorbed_id, survivor_id
            )
            out["redirected_review_case_ids"] = redirected_ids
        out["survivor_person_id"] = survivor_id
        out["golden_profile_selections"] = golden_profile_selections

    if action_type in (ApiReviewActionType.MANUAL_NO_MATCH.value, ApiReviewActionType.REJECT.value):
        await tx.run(MARK_REVIEW_SALES_RECORD_UNRESOLVED, review_case_id=review_case_id)

    return out
