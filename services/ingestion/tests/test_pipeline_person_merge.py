"""Person-pair auto-merge: threshold, survivor selection, merge execution."""

from __future__ import annotations

from src.graph import queries
from src.matching.pair_score import PERSON_PAIR_AUTO_MERGE


def test_person_pair_auto_merge_threshold_is_060() -> None:
    assert PERSON_PAIR_AUTO_MERGE == 0.60


def test_person_merge_query_constants_exist() -> None:
    assert "left_status" in queries.FETCH_PAIR_MERGE_ATTRS
    assert "right_status" in queries.FETCH_PAIR_MERGE_ATTRS
    assert "cancelled_superseded" in queries.CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED
    assert "ABOUT_LEFT" in queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT
    assert "ABOUT_RIGHT" in queries.REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT
    assert "source_record" in queries.REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED
