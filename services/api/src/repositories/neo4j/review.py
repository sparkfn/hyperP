"""Neo4j implementation of ReviewRepository."""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.client import get_session
from src.graph.converters import GraphRecord, to_optional_str, to_str
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
    LIST_REVIEW_CASES,
    build_review_action_cypher,
)
from src.repositories.protocols.review import ActionResult, AssignResult, ReviewListFilters
from src.types import ApiReviewActionType, ReviewCaseDetail, ReviewCaseSummary

from ._utils import record_to_dict


class Neo4jReviewRepository:
    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], bool]:
        async with get_session() as session:
            result = await session.run(
                LIST_REVIEW_CASES,
                queue_state=filters.get("queue_state"),
                assigned_to=filters.get("assigned_to"),
                priority_lte=filters.get("priority_lte"),
                skip=skip,
                limit=limit + 1,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > limit
        return [map_review_case_summary(rec) for rec in records[:limit]], has_more

    async def get_by_id(self, review_case_id: str) -> ReviewCaseDetail | None:
        async with get_session() as session:
            result = await session.run(GET_REVIEW_CASE, review_case_id=review_case_id)
            record = await result.single()
        if record is None:
            return None
        return map_review_case_detail(record_to_dict(record.keys(), list(record.values())))

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
    ) -> ActionResult | None:
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
            )

        if result is None:
            return None

        # Recompute golden profile for the surviving person after a merge
        survivor_id = to_optional_str(result.get("survivor_person_id"))
        if action_type == ApiReviewActionType.MERGE.value and survivor_id:
            async with get_session(write=True) as session:
                await session.execute_write(recompute_golden_profile_tx, survivor_id)

        return result


async def _assign_tx(
    tx: AsyncManagedTransaction, review_case_id: str, assigned_to: str
) -> GraphRecord | None:
    result = await tx.run(
        ASSIGN_REVIEW_CASE, review_case_id=review_case_id, assigned_to=assigned_to
    )
    record = await result.single()
    if record is None:
        return None
    return dict(record["review_case"])


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
) -> ActionResult | None:
    absorbed_id: str | None = None
    survivor_id: str | None = None

    if action_type == ApiReviewActionType.MERGE.value:
        persons_result = await tx.run(GET_PERSONS_FOR_REVIEW_MERGE, review_case_id=review_case_id)
        persons_record = await persons_result.single()
        if persons_record is None:
            return ActionResult(merge_not_applicable=True)

        left_id = to_str(persons_record["left_person_id"])
        right_id = to_str(persons_record["right_person_id"])

        if survivor_person_id == right_id:
            survivor_id, absorbed_id = right_id, left_id
        else:
            survivor_id, absorbed_id = left_id, right_id

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
        action_type=action_type,
        notes=notes,
        resolution=resolution,
        follow_up_at=follow_up_at,
        actor_id=actor_id,
    )
    record = await result.single()
    if record is None:
        return None

    rc = dict(record["review_case"])
    out = ActionResult(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        resolution=to_optional_str(rc.get("resolution")),
    )

    if action_type == ApiReviewActionType.MANUAL_NO_MATCH.value:
        await tx.run(
            CREATE_NO_MATCH_LOCK_FROM_REVIEW,
            review_case_id=review_case_id,
            notes=notes or "Manual no-match from review",
            actor_id=actor_id,
        )
    elif action_type == ApiReviewActionType.MERGE.value and absorbed_id and survivor_id:
        await tx.run(
            EXECUTE_MANUAL_MERGE,
            from_id=absorbed_id,
            to_id=survivor_id,
            reason=notes or "Review merge",
            actor_id=actor_id,
        )
        out["survivor_person_id"] = survivor_id

    return out
