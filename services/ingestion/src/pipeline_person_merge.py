"""Person-pair auto-merge execution inside the ingestion transaction."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction

from src.golden_profile import compute_golden_profile
from src.graph import queries


@dataclass(frozen=True)
class PairPersonAttrs:
    """Attributes used to select the surviving person deterministically."""

    person_id: str
    status: str
    profile_completeness_score: float
    created_at: str


def fetch_pair_attrs(
    tx: ManagedTransaction, left_person_id: str, right_person_id: str
) -> tuple[PairPersonAttrs, PairPersonAttrs] | None:
    """Fetch both persons' merge-relevant attributes."""
    record = tx.run(
        queries.FETCH_PAIR_MERGE_ATTRS,
        left_person_id=left_person_id,
        right_person_id=right_person_id,
    ).single()
    if record is None:
        return None
    values = (
        record.get("left_person_id"),
        record.get("left_status"),
        record.get("left_completeness"),
        record.get("left_created_at"),
        record.get("right_person_id"),
        record.get("right_status"),
        record.get("right_completeness"),
        record.get("right_created_at"),
    )
    if not (
        isinstance(values[0], str)
        and isinstance(values[1], str)
        and isinstance(values[2], (int, float))
        and isinstance(values[3], str)
        and isinstance(values[4], str)
        and isinstance(values[5], str)
        and isinstance(values[6], (int, float))
        and isinstance(values[7], str)
    ):
        return None
    return (
        PairPersonAttrs(values[0], values[1], float(values[2]), values[3]),
        PairPersonAttrs(values[4], values[5], float(values[6]), values[7]),
    )


def select_survivor(left: PairPersonAttrs, right: PairPersonAttrs) -> tuple[str, str]:
    """Return ``(survivor_id, absorbed_id)`` using stable tie-breakers."""
    if left.profile_completeness_score != right.profile_completeness_score:
        winner = (
            left if left.profile_completeness_score > right.profile_completeness_score else right
        )
    elif left.created_at != right.created_at:
        winner = left if left.created_at < right.created_at else right
    else:
        winner = left if left.person_id < right.person_id else right
    loser = right if winner is left else left
    return winner.person_id, loser.person_id


def merge_person_pair(
    tx: ManagedTransaction,
    *,
    absorbed_id: str,
    survivor_id: str,
    match_decision_id: str,
    reason: str,
) -> str:
    """Rewire an absorbed person into its survivor and return the merge-event id."""
    record = tx.run(
        queries.CREATE_MERGE_EVENT_AUTO_MERGE,
        from_person_id=absorbed_id,
        to_person_id=survivor_id,
        reason=reason,
    ).single()
    assert record is not None, "CREATE_MERGE_EVENT_AUTO_MERGE must return a row"
    merge_event_id = record["merge_event_id"]
    assert isinstance(merge_event_id, str)
    tx.run(
        queries.LINK_MERGE_EVENT_TRIGGERED_BY,
        merge_event_id=merge_event_id,
        match_decision_id=match_decision_id,
    )
    for query in (
        queries.REWIRE_LINKED_TO,
        queries.REWIRE_IDENTIFIED_BY,
        queries.REWIRE_LIVES_AT,
        queries.REWIRE_HAS_FACT,
        queries.REWIRE_KNOWS_OUT,
        queries.REWIRE_KNOWS_IN,
        queries.REWIRE_PURCHASED,
    ):
        tx.run(query, absorbed_id=absorbed_id, survivor_id=survivor_id)
    tx.run(queries.MARK_PERSON_MERGED, absorbed_id=absorbed_id)
    tx.run(
        queries.CREATE_MERGED_INTO,
        absorbed_id=absorbed_id,
        survivor_id=survivor_id,
        merge_event_id=merge_event_id,
    )
    tx.run(queries.PATH_COMPRESS_MERGED_INTO, absorbed_id=absorbed_id, survivor_id=survivor_id)
    for query in (
        queries.CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED,
        queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT,
        queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT,
        queries.REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED,
    ):
        tx.run(
            query,
            absorbed_id=absorbed_id,
            survivor_id=survivor_id,
            merge_event_id=merge_event_id,
        )
    compute_golden_profile(tx, survivor_id)
    return merge_event_id
