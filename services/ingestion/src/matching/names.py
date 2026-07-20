"""Name-matching helpers shared by the deterministic gate and heuristic scorer.

Kept separate from :mod:`src.matching.similarity`, which owns the legacy Jaro contract
and RapidFuzz-backed OSA primitive, because these helpers know about the domain
``NormalizedAttribute`` shape and the fields that count as a person's name.
"""

from __future__ import annotations

from src.matching.similarity import jaro_winkler_similarity
from src.models import NormalizedAttribute

#: Incoming attribute names treated as a person's name for matching.
NAME_INCOMING_FIELDS: tuple[str, ...] = ("full_name", "preferred_name", "legal_name")

#: Jaro-Winkler at/above this is a partial-or-better name match; below it is a
#: strong mismatch. Single source of truth shared by the deterministic bankruptcy
#: gate and the heuristic name band.
NAME_PARTIAL_THRESHOLD: float = 0.50


def incoming_names(attributes: list[NormalizedAttribute]) -> list[str]:
    """Return the name strings carried by an incoming record's attributes."""
    return [a.attribute_value for a in attributes if a.attribute_name in NAME_INCOMING_FIELDS]


def best_name_similarity(incoming: list[str], candidate_names: list[str]) -> float:
    """Best Jaro-Winkler similarity across the incoming x candidate name pairs."""
    best = 0.0
    for inc in incoming:
        for cand in candidate_names:
            best = max(best, jaro_winkler_similarity(inc, cand))
    return best


def is_partial_name_match(
    attributes: list[NormalizedAttribute],
    candidate_names: list[str],
) -> bool | None:
    """Partial-name verdict for a pair.

    Returns ``True`` / ``False`` when both sides carry a name (best JW >=/<
    ``NAME_PARTIAL_THRESHOLD``); ``None`` when either side has no name, so callers
    can decide a name-absent fallback.
    """
    inc = incoming_names(attributes)
    if not inc or not candidate_names:
        return None
    return best_name_similarity(inc, candidate_names) >= NAME_PARTIAL_THRESHOLD
