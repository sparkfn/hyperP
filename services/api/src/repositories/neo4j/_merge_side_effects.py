"""Review-case side-effects of a person merge, and their unmerge reversal.

A person merge must keep the review queue consistent:
- other open person↔person cases referencing the absorbed person are closed
  (the absorbed person no longer exists as a distinct reviewable person);
- open record↔person cases pointing at the absorbed person are redirected to
  the survivor.

Both mutations are stamped with the ``merge_event_id`` so an unmerge can revert
exactly the cases this merge changed — but only where a human has not acted on
the case since the merge (the revert queries are state-guarded).

Centralized here because two paths execute merges: the review-merge action
(``review.py:_action_tx``) and the direct admin merge (``merge.py:_manual_merge_tx``).
"""

from __future__ import annotations

from neo4j import AsyncManagedTransaction

from src.graph.queries import (
    CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
    REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    REVERT_PERSON_PAIR_CASE_CLOSURES,
    REVERT_RECORD_PERSON_CASE_REDIRECTS,
)


async def apply_merge_review_side_effects(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
    absorbed_id: str,
    survivor_id: str,
) -> None:
    """Close/redirect review cases affected by a merge; runs in the merge tx."""
    await tx.run(
        CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        merge_event_id=merge_event_id,
    )
    await tx.run(
        REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )


async def revert_merge_review_side_effects(
    tx: AsyncManagedTransaction,
    merge_event_id: str,
) -> None:
    """Revert merge side-effects on unmerge for cases untouched since the merge."""
    await tx.run(REVERT_RECORD_PERSON_CASE_REDIRECTS, merge_event_id=merge_event_id)
    await tx.run(REVERT_PERSON_PAIR_CASE_CLOSURES, merge_event_id=merge_event_id)
