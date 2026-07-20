"""String-similarity compatibility helpers used by the heuristic scorer.

RapidFuzz provides OSA distance. Jaro retains the legacy matching order because
RapidFuzz resolves repeated-character matches differently, which can change
domain decisions at existing thresholds.
"""

from __future__ import annotations

from rapidfuzz.distance import OSA


def jaro_similarity(s1: str, s2: str) -> float:
    """Return Jaro similarity using the established matching order."""
    if s1 == s2:
        return 1.0
    len_s1, len_s2 = len(s1), len(s2)
    if len_s1 == 0 or len_s2 == 0:
        return 0.0

    match_distance = max(0, max(len_s1, len_s2) // 2 - 1)
    s1_matches = [False] * len_s1
    s2_matches = [False] * len_s2
    matches = 0
    transpositions = 0

    for index1 in range(len_s1):
        start = max(0, index1 - match_distance)
        end = min(index1 + match_distance + 1, len_s2)
        for index2 in range(start, end):
            if s2_matches[index2] or s1[index1] != s2[index2]:
                continue
            s1_matches[index1] = True
            s2_matches[index2] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    index2 = 0
    for index1 in range(len_s1):
        if not s1_matches[index1]:
            continue
        while not s2_matches[index2]:
            index2 += 1
        if s1[index1] != s2[index2]:
            transpositions += 1
        index2 += 1

    return (matches / len_s1 + matches / len_s2 + (matches - transpositions / 2) / matches) / 3.0


def jaro_winkler_similarity(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Return normalized Jaro-Winkler similarity with the legacy prefix policy."""
    normalized1 = s1.lower().strip()
    normalized2 = s2.lower().strip()
    jaro = jaro_similarity(normalized1, normalized2)

    prefix_len = 0
    for index in range(min(len(normalized1), len(normalized2), 4)):
        if normalized1[index] != normalized2[index]:
            break
        prefix_len += 1

    return jaro + prefix_len * prefix_weight * (1 - jaro)


def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """Return optimal string alignment distance, preserving the public API name."""
    return OSA.distance(s1, s2)
