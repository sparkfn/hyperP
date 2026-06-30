"""Unit tests for stdlib string-similarity helpers."""

from __future__ import annotations

from src.matching.similarity import damerau_levenshtein_distance


def test_identical_strings_have_zero_distance() -> None:
    assert damerau_levenshtein_distance("96427694", "96427694") == 0


def test_single_substitution_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "96427699") == 1


def test_adjacent_transposition_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "96472694") == 1


def test_single_insertion_is_distance_one() -> None:
    assert damerau_levenshtein_distance("9642769", "96427694") == 1


def test_single_deletion_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "9642769") == 1


def test_two_substitutions_is_distance_two() -> None:
    assert damerau_levenshtein_distance("96427694", "96427799") == 2


def test_empty_strings() -> None:
    assert damerau_levenshtein_distance("", "") == 0
    assert damerau_levenshtein_distance("", "a") == 1
    assert damerau_levenshtein_distance("a", "") == 1
