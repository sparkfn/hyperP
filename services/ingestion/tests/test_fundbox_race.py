"""Fundbox race/ethnicity extraction and normalization tests."""

from __future__ import annotations

from src.connectors.fundbox.builders import _norm_race


def test_norm_race_title_cases_and_trims() -> None:
    assert _norm_race("MALAY") == "Malay"
    assert _norm_race("  chinese ") == "Chinese"
    assert _norm_race("BOYANESE") == "Boyanese"


def test_norm_race_none_for_empty_or_junk() -> None:
    assert _norm_race(None) is None
    assert _norm_race("") is None
    assert _norm_race("   ") is None
