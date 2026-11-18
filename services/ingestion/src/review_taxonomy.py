"""Exception-only review taxonomy for v2 identity matching (plan §2.5).

Heuristic scoring never emits REVIEW in v2: only the four taxonomy reasons
open cases, everything else parks or suppresses. ``case_key`` makes one case
per logical exception recur across records and ingests.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Literal, cast

from src.models import MatchResult

ReviewReason = Literal[
    "anchored_ambiguity", "nric_contradiction", "crm_ambiguity", "continuity_reassignment"
]

_TAXONOMY_REASONS: frozenset[str] = frozenset(
    {"anchored_ambiguity", "nric_contradiction", "crm_ambiguity", "continuity_reassignment"}
)


def exception_reason(match_result: MatchResult) -> ReviewReason | None:
    """Return the taxonomy reason for a REVIEW, else ``None`` (suppress).

    The explicit ``review_reason`` wins; otherwise the existing
    ``classify_created_case_reason`` is mapped through the taxonomy.
    """
    reason = match_result.review_reason
    if reason is not None and reason in _TAXONOMY_REASONS:
        return cast(ReviewReason, reason)
    # Deferred: pipeline_writes imports this module for the exception gate.
    from src.pipeline_writes import classify_created_case_reason

    classified = classify_created_case_reason(match_result)
    if classified in _TAXONOMY_REASONS:
        return cast(ReviewReason, classified)
    return None


def case_key(reason: ReviewReason, record_identity: str | None, person_ids: Sequence[str]) -> str:
    """Return the sha1 case key over a per-reason field set.

    ``anchored_ambiguity`` keys on the sorted person ids alone (one case across
    records); ``nric_contradiction`` keys on profile id + holder person id in
    the caller-supplied order; ``crm_ambiguity``/``continuity_reassignment``
    key on the record identity plus the sorted person ids.
    """
    if reason == "anchored_ambiguity":
        parts = [reason, *sorted(person_ids)]
    elif reason == "nric_contradiction":
        parts = [reason, *person_ids]
    else:
        parts = [reason, record_identity or "", *sorted(person_ids)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def record_identity_for_envelope(
    *, source_system: str, source_instance_id: str | None, source_record_id: str
) -> str:
    """Return the version-independent logical identity of a source record."""
    return "|".join([source_system, source_instance_id or "", source_record_id])
