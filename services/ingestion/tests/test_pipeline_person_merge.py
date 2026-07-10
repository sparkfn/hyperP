"""Person-pair auto-merge: threshold, survivor selection, merge execution."""

from __future__ import annotations

from src.matching.pair_score import PERSON_PAIR_AUTO_MERGE


def test_person_pair_auto_merge_threshold_is_060() -> None:
    assert PERSON_PAIR_AUTO_MERGE == 0.60
