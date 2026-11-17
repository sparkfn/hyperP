"""Pure v2 weighted identifier scoring (plan §2.3).

Every matching link contributes ``signal × link_weight × IDF``: the signal
reuses the legacy verified/unverified phone/email constants, ``link_weight``
is the C4 link score (a missing weight means a pre-C4 link and is treated
as unweighted), and ``IDF`` discounts widely-shared identifiers.
"""

from __future__ import annotations

import math


def idf(participant_count: int | None) -> float:
    """Return ``1 / (1 + ln(max(n, 1)))``; missing counts assume a unique link."""
    count = participant_count if participant_count is not None and participant_count > 0 else 1
    return 1.0 / (1.0 + math.log(count))


def weighted_signal(
    signal: float, link_weight: float | None, participant_count: int | None
) -> float:
    """Weight one identifier signal by its link weight and exclusivity."""
    weight = 1.0 if link_weight is None else link_weight
    return signal * weight * idf(participant_count)
