"""Neo4j implementation of MergeRepository."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from neo4j import AsyncManagedTransaction

from src.celery_client import enqueue_match_recalculation
from src.graph.client import get_session
from src.graph.converters import to_str
from src.graph.crm_deal_count import recompute_person_crm_deal_counts
from src.graph.golden_profile import recompute_golden_profile_tx
from src.graph.queries import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_EXISTING_LOCK,
    CHECK_NO_MATCH_LOCK,
    CREATE_PERSON_PAIR_LOCK,
    CREATE_UNMERGE_AUDIT,
    DELETE_LOCK,
    EXECUTE_MANUAL_MERGE,
    FLAG_AFFECTED_RECORDS_FOR_REVIEW,
    GET_ADDRESS_BY_NORMALIZED,
    GET_UNMERGE_TARGET,
    REVERT_MERGE,
    UPDATE_GOLDEN_FIELDS,
)
from src.identity_link_revisions import append_merge_affected_revisions
from src.repositories.neo4j._merge_side_effects import (
    apply_merge_review_side_effects,
    revert_merge_review_side_effects,
)
from src.repositories.protocols.merge import GoldenProfileSelection, MergeOutcome


@dataclass
class _UnmergeResult:
    absorbed_id: str
    current_survivor_id: str
    reverted_review_case_ids: list[str]


logger = logging.getLogger(__name__)


def _ordered_pair(left: str, right: str) -> tuple[str, str]:
    return (left, right) if left < right else (right, left)


class Neo4jMergeRepository:
    async def manual_merge(
        self,
        from_id: str,
        to_id: str,
        reason: str,
        actor_id: str,
        golden_profile_selections: list[GoldenProfileSelection],
    ) -> MergeOutcome:
        if not are_valid_golden_profile_selections(golden_profile_selections):
            return MergeOutcome(not_found=True)
        async with get_session(write=True) as session:
            outcome = await session.execute_write(
                _manual_merge_with_profile_tx,
                from_id,
                to_id,
                reason,
                actor_id,
                golden_profile_selections,
            )
            enqueue_match_recalculation(outcome.redirected_review_case_ids)
            return outcome

    async def unmerge(
        self, merge_event_id: str, reason: str, actor_id: str
    ) -> tuple[str, str] | None:
        async with get_session(write=True) as session:
            result = await session.execute_write(
                _unmerge_with_profiles_tx, merge_event_id, reason, actor_id
            )
            if result is not None:
                enqueue_match_recalculation(result.reverted_review_case_ids)
            return (result.absorbed_id, result.current_survivor_id) if result is not None else None

    async def create_lock(
        self,
        left: str,
        right: str,
        lock_type: str,
        reason: str,
        expires_at: str | None,
        actor_id: str,
    ) -> tuple[str, str | None]:
        async with get_session(write=True) as session:
            return await session.execute_write(
                _create_lock_tx, left, right, lock_type, reason, expires_at, actor_id
            )

    async def delete_lock(self, lock_id: str) -> bool:
        async with get_session(write=True) as session:
            return await session.execute_write(_delete_lock_tx, lock_id)


async def _manual_merge_tx(
    tx: AsyncManagedTransaction, from_id: str, to_id: str, reason: str, actor_id: str
) -> MergeOutcome:
    left, right = _ordered_pair(from_id, to_id)
    lock_result = await tx.run(CHECK_NO_MATCH_LOCK, left=left, right=right)
    lock_record = await lock_result.single()
    if lock_record is not None and bool(lock_record["is_locked"]):
        return MergeOutcome(blocked=True)

    person_result = await tx.run(CHECK_BOTH_PERSONS_ACTIVE, from_id=from_id, to_id=to_id)
    if await person_result.single() is None:
        return MergeOutcome(not_found=True)

    merge_result = await tx.run(
        EXECUTE_MANUAL_MERGE,
        from_id=from_id,
        to_id=to_id,
        left=left,
        right=right,
        reason=reason,
        actor_id=actor_id,
    )
    record = await merge_result.single()
    if record is None:
        return MergeOutcome(not_found=True)
    merge_event_id = to_str(record["merge_event_id"])
    await recompute_person_crm_deal_counts(tx, [from_id, to_id])
    redirected_ids = await apply_merge_review_side_effects(tx, merge_event_id, from_id, to_id)
    await append_merge_affected_revisions(
        tx,
        merge_event_id=merge_event_id,
        survivor_person_id=to_id,
        resolution_kind="person_merge",
        cause_prefix=f"person-merge:{merge_event_id}",
        effective_at=str(record["created_at"]),
    )
    return MergeOutcome(merge_event_id=merge_event_id, redirected_review_case_ids=redirected_ids)


async def _manual_merge_with_profile_tx(
    tx: AsyncManagedTransaction,
    from_id: str,
    to_id: str,
    reason: str,
    actor_id: str,
    golden_profile_selections: list[GoldenProfileSelection],
) -> MergeOutcome:
    outcome = await _manual_merge_tx(tx, from_id, to_id, reason, actor_id)
    if outcome.merge_event_id is None:
        return outcome
    await recompute_golden_profile_tx(tx, to_id, False)
    if golden_profile_selections:
        await _apply_golden_profile_selections_tx(tx, to_id, golden_profile_selections)
    return outcome


IDENTIFIER_FIELD_BY_TYPE: dict[str, str] = {
    "phone": "preferred_phone",
    "mobile": "preferred_phone",
    "email": "preferred_email",
    "nric": "preferred_nric",
}

VALID_LITERAL_GOLDEN_FIELDS: frozenset[str] = frozenset(
    {
        "preferred_full_name",
        "preferred_dob",
        "preferred_phone",
        "preferred_email",
        "preferred_nric",
    }
)


FACT_FIELDS: frozenset[str] = frozenset(
    {"preferred_full_name", "preferred_dob", "preferred_phone", "preferred_email"}
)


def _is_valid_golden_profile_selection(selection: GoldenProfileSelection) -> bool:
    source_kind = selection["source_kind"]
    field_name = selection["field_name"]
    if source_kind == "identifier":
        identifier_type = selection["identifier_type"]
        if identifier_type is None:
            return False
        return IDENTIFIER_FIELD_BY_TYPE.get(identifier_type.lower()) == field_name
    if source_kind == "source_record_fact":
        return field_name in FACT_FIELDS and selection["source_record_pk"] is not None
    if source_kind == "address":
        return field_name == "preferred_address" and selection["selected_value"].strip() != ""
    if source_kind == "literal":
        return (
            field_name in VALID_LITERAL_GOLDEN_FIELDS and selection["selected_value"].strip() != ""
        )
    return False


def are_valid_golden_profile_selections(selections: list[GoldenProfileSelection]) -> bool:
    return all(_is_valid_golden_profile_selection(selection) for selection in selections)


async def _apply_golden_profile_selections_tx(
    tx: AsyncManagedTransaction,
    person_id: str,
    selections: list[GoldenProfileSelection],
) -> str:
    updates: list[dict[str, str]] = []
    for selection in selections:
        field_name = selection["field_name"]
        if field_name == "preferred_address":
            normalized = " ".join(selection["selected_value"].strip().lower().split())
            record = await (
                await tx.run(GET_ADDRESS_BY_NORMALIZED, normalized_full=normalized)
            ).single()
            if record is None:
                continue
            updates.append(
                {
                    "field_name": "preferred_address_id",
                    "value": to_str(record["address_id"]),
                }
            )
        else:
            updates.append({"field_name": field_name, "value": selection["selected_value"]})
    if updates:
        await tx.run(
            UPDATE_GOLDEN_FIELDS,
            person_id=person_id,
            updates=updates,
            invalidate_analysis=False,
        )
    return "ok"


async def _unmerge_tx(
    tx: AsyncManagedTransaction, merge_event_id: str, reason: str, actor_id: str
) -> _UnmergeResult | None:
    target_result = await tx.run(GET_UNMERGE_TARGET, merge_event_id=merge_event_id)
    target = await target_result.single()
    if target is None:
        return None
    absorbed_id = to_str(target["absorbed_id"])
    survivor_id = to_str(target["survivor_id"])

    revert_result = await tx.run(
        REVERT_MERGE,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    revert_record = await revert_result.single()
    if revert_record is None or int(revert_record["removed_count"]) == 0:
        return None
    current_survivor_id = to_str(revert_record["current_survivor_id"])
    await recompute_person_crm_deal_counts(tx, [absorbed_id, current_survivor_id])
    audit_result = await tx.run(
        CREATE_UNMERGE_AUDIT,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        reason=reason,
        metadata=json.dumps({"original_merge_event_id": merge_event_id}),
        actor_id=actor_id,
    )
    audit_record = await audit_result.single()
    if audit_record is None:
        raise RuntimeError("unmerge audit was not created")
    await tx.run(FLAG_AFFECTED_RECORDS_FOR_REVIEW, merge_event_id=merge_event_id)
    await append_merge_affected_revisions(
        tx,
        merge_event_id=merge_event_id,
        survivor_person_id=None,
        resolution_kind="person_unmerge",
        cause_prefix=f"person-unmerge:{to_str(audit_record['merge_event_id'])}",
        effective_at=to_str(audit_record["created_at"]),
    )
    reverted_ids = await revert_merge_review_side_effects(tx, merge_event_id)
    return _UnmergeResult(
        absorbed_id=absorbed_id,
        current_survivor_id=current_survivor_id,
        reverted_review_case_ids=reverted_ids,
    )


async def _unmerge_with_profiles_tx(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
    reason: str,
    actor_id: str,
) -> _UnmergeResult | None:
    result = await _unmerge_tx(tx, merge_event_id, reason, actor_id)
    if result is None:
        return None
    await recompute_golden_profile_tx(tx, result.absorbed_id, False)
    await recompute_golden_profile_tx(tx, result.current_survivor_id, False)
    return result


async def _create_lock_tx(
    tx: AsyncManagedTransaction,
    left: str,
    right: str,
    lock_type: str,
    reason: str,
    expires_at: str | None,
    actor_id: str,
) -> tuple[str, str | None]:
    existing = await tx.run(CHECK_EXISTING_LOCK, left=left, right=right)
    existing_record = await existing.single()
    if existing_record is not None:
        return "conflict", to_str(existing_record["lock_id"])

    result = await tx.run(
        CREATE_PERSON_PAIR_LOCK,
        left=left,
        right=right,
        lock_type=lock_type,
        reason=reason,
        expires_at=expires_at,
        actor_id=actor_id,
    )
    record = await result.single()
    if record is None:
        return "not_found", None
    return "ok", to_str(record["lock_id"])


async def _delete_lock_tx(tx: AsyncManagedTransaction, lock_id: str) -> bool:
    result = await tx.run(DELETE_LOCK, lock_id=lock_id)
    record = await result.single()
    return record is not None
