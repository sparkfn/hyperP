"""Review-case side-effects of a person merge, and their unmerge reversal.

A person merge must keep the review queue consistent:
- other open person↔person cases referencing the absorbed person are
  redirected to the survivor (the absorbed side is repointed); the narrow
  edge case where the case was literally absorbed↔survivor (already moot)
  is cancelled instead;
- open record↔person cases pointing at the absorbed person are redirected to
  the survivor.

Both mutations are stamped with the ``merge_event_id`` so an unmerge can revert
exactly the cases this merge changed — but only where a human has not acted on
the case since the merge (the revert queries are state-guarded).

Centralized here because two paths
(`POST /v1/persons/manual-merge` and review-case merge actions) share the same
side-effects and the same unmerge reversal.
"""

from __future__ import annotations

from neo4j import AsyncManagedTransaction, AsyncResult

from src.graph.converters import to_str
from src.graph.queries import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
    REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_PERSON_PAIR_REDIRECTS_LEFT,
    REVERT_PERSON_PAIR_REDIRECTS_RIGHT,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)


async def _collect_review_case_ids(result: AsyncResult) -> list[str]:
    """Extract ``review_case_id`` values from a Neo4j result."""
    ids: list[str] = []
    async for record in result:
        value = record.get("review_case_id")
        if value is not None:
            ids.append(to_str(value))
    return ids


async def apply_merge_review_side_effects(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
    absorbed_id: str,
    survivor_id: str,
) -> list[str]:
    """Redirect review cases and return the redirected person-pair case IDs.

    The returned IDs are used by callers to queue match recalculation tasks
    for each affected pair-audit review case after the merge commits.
    """
    await tx.run(
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    left_result = await tx.run(
        REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    right_result = await tx.run(
        REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    await tx.run(
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    redirected_ids = await _collect_review_case_ids(left_result)
    redirected_ids.extend(await _collect_review_case_ids(right_result))
    return redirected_ids


async def revert_merge_review_side_effects(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
) -> list[str]:
    """Revert merge side-effects on unmerge for cases untouched since the merge.

    Returns the review_case_ids of person-pair cases that were reverted so callers
    can queue match recalculation tasks after the unmerge commits.
    """
    await tx.run(REVERT_RECORD_PERSON_CASE_REDIRECTS, merge_event_id=merge_event_id)
    left_result = await tx.run(REVERT_PERSON_PAIR_REDIRECTS_LEFT, merge_event_id=merge_event_id)
    right_result = await tx.run(REVERT_PERSON_PAIR_REDIRECTS_RIGHT, merge_event_id=merge_event_id)
    await tx.run(REVERT_PERSON_PAIR_CASE_CLOSURES, merge_event_id=merge_event_id)
    reverted_ids = await _collect_review_case_ids(left_result)
    reverted_ids.extend(await _collect_review_case_ids(right_result))
    return reverted_ids
