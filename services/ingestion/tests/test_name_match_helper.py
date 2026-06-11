"""Shared name-match helper used by both the deterministic gate and heuristic."""

from __future__ import annotations

from src.matching.names import (
    NAME_PARTIAL_THRESHOLD,
    best_name_similarity,
    incoming_names,
    is_partial_name_match,
)
from src.models import NormalizedAttribute, QualityFlag


def _attr(name: str, value: str) -> NormalizedAttribute:
    return NormalizedAttribute(
        attribute_name=name, attribute_value=value, quality_flag=QualityFlag.VALID
    )


def test_incoming_names_picks_name_fields_only() -> None:
    attrs = [_attr("full_name", "Ada Lovelace"), _attr("dob", "1990-01-01")]
    assert incoming_names(attrs) == ["Ada Lovelace"]


def test_best_similarity_is_one_for_exact() -> None:
    assert best_name_similarity(["Ada Lovelace"], ["Ada Lovelace"]) == 1.0


def test_partial_match_true_above_threshold() -> None:
    assert is_partial_name_match([_attr("full_name", "Li Wei")], ["Wei Li"]) is True
    assert NAME_PARTIAL_THRESHOLD == 0.50


def test_partial_match_false_for_strong_mismatch() -> None:
    assert is_partial_name_match([_attr("full_name", "Ada Lovelace")], ["Kok Pin"]) is False


def test_partial_match_none_when_a_side_lacks_name() -> None:
    assert is_partial_name_match([], ["Ada Lovelace"]) is None
    assert is_partial_name_match([_attr("full_name", "Ada")], []) is None
