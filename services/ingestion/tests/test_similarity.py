"""Unit tests for the string-similarity compatibility boundary."""

from __future__ import annotations

import pytest
import src.matching.similarity as similarity
from src.matching.similarity import (
    damerau_levenshtein_distance,
    jaro_similarity,
    jaro_winkler_similarity,
)


def test_damerau_levenshtein_delegates_to_rapidfuzz_osa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class OsaStub:
        @staticmethod
        def distance(s1: str, s2: str) -> int:
            calls.append((s1, s2))
            return 7

    monkeypatch.setattr(similarity, "OSA", OsaStub, raising=False)

    assert damerau_levenshtein_distance("CA", "ABC") == 7
    assert calls == [("CA", "ABC")]


@pytest.mark.parametrize(
    ("s1", "s2", "expected"),
    [
        ("", "", 1.0),
        ("", "abc", 0.0),
        ("abc", "xyz", 0.0),
        ("MARTHA", "MARHTA", 0.9444444444444445),
        ("DIXON", "DICKSONX", 0.7666666666666666),
        ("aaabc", "aabcaa", 0.8444444444444444),
    ],
)
def test_jaro_similarity_characterization(s1: str, s2: str, expected: float) -> None:
    assert jaro_similarity(s1, s2) == pytest.approx(expected)


def test_jaro_winkler_preserves_case_and_whitespace_normalization() -> None:
    assert jaro_winkler_similarity("  MARTHA  ", "marhta") == pytest.approx(0.9611111111111111)


def test_jaro_winkler_handles_unicode_codepoints() -> None:
    assert jaro_winkler_similarity("  JOSÉ  ", "josé") == 1.0


def test_jaro_winkler_preserves_legacy_boost_at_name_threshold() -> None:
    assert jaro_winkler_similarity("Axxxxx", "Ayyyyy") == pytest.approx(0.5)


def test_jaro_winkler_preserves_repeated_character_matching_order() -> None:
    assert jaro_winkler_similarity("aaabc", "aabcaa") == pytest.approx(0.8755555555555555)


def test_identical_strings_have_zero_distance() -> None:
    assert damerau_levenshtein_distance("96427694", "96427694") == 0


def test_single_substitution_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "96427699") == 1


def test_adjacent_transposition_is_distance_one() -> None:
    assert damerau_levenshtein_distance("96427694", "96472694") == 1


def test_overlapping_transpositions_use_osa_semantics() -> None:
    assert damerau_levenshtein_distance("CA", "ABC") == 3


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
