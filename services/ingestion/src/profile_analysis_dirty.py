"""Typed transaction helper for accepted profile-analysis input changes."""

from __future__ import annotations

from collections.abc import Iterable

from neo4j import ManagedTransaction

from src.graph import queries


def _unique_nonempty(values: Iterable[str]) -> list[str]:
    return sorted({value for value in values if value})


def mark_profile_analysis_dirty(
    tx: ManagedTransaction,
    *,
    source_record_pks: Iterable[str] = (),
    person_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Increment each affected active Person once in the caller's transaction."""
    source_keys = _unique_nonempty(source_record_pks)
    direct_ids = _unique_nonempty(person_ids)
    if not source_keys and not direct_ids:
        return ()
    rows = tx.run(
        queries.MARK_PROFILE_ANALYSIS_DIRTY,
        source_record_pks=source_keys,
        person_ids=direct_ids,
    )
    return tuple(str(row["person_id"]) for row in rows)
