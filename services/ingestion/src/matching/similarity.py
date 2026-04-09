"""String similarity helpers used by the heuristic scorer.

Implementations are stdlib-only — no external dependencies — and are kept in
their own module so they can be unit-tested in isolation and replaced (e.g.
with the C-accelerated ``rapidfuzz`` package) without touching the engine.
"""

from __future__ import annotations


def jaro_similarity(s1: str, s2: str) -> float:
    """Compute the Jaro similarity between two strings.

    Returns a value in ``[0.0, 1.0]``. ``1.0`` for identical strings, ``0.0``
    for strings with no matching characters within the Jaro match window.
    """
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

    for i in range(len_s1):
        start = max(0, i - match_distance)
        end = min(i + match_distance + 1, len_s2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(len_s1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    return (
        matches / len_s1
        + matches / len_s2
        + (matches - transpositions / 2) / matches
    ) / 3.0


def jaro_winkler_similarity(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    """Compute the Jaro–Winkler similarity between two strings.

    Case-insensitive and whitespace-trimmed. ``prefix_weight`` boosts strings
    that share a common prefix (up to 4 characters), which is useful for
    short tokens like names.
    """
    s1 = s1.lower().strip()
    s2 = s2.lower().strip()

    jaro = jaro_similarity(s1, s2)

    prefix_len = 0
    for i in range(min(len(s1), len(s2), 4)):
        if s1[i] == s2[i]:
            prefix_len += 1
        else:
            break

    return jaro + prefix_len * prefix_weight * (1 - jaro)
