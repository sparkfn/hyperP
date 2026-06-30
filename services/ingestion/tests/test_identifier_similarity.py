"""Phone/email near-match (approximate identifier) scoring helpers."""

from __future__ import annotations

from src.matching.identifier_similarity import email_near_match, phone_near_match

# --- phone_near_match -------------------------------------------------------


def test_phone_single_digit_substitution_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+6591234568") is True


def test_phone_adjacent_digit_transposition_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+6591234657") is True


def test_phone_single_digit_insertion_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+65912345670") is True


def test_phone_single_digit_deletion_is_near_match() -> None:
    assert phone_near_match("+6591234567", "+659123456") is True


def test_phone_two_digit_difference_is_not_near_match() -> None:
    assert phone_near_match("+6591234567", "+6591234599") is False


def test_phone_identical_numbers_are_not_near_match() -> None:
    # Exact equality is handled by the exact-match pass, not this function.
    assert phone_near_match("+6591234567", "+6591234567") is False


def test_phone_cross_region_one_digit_difference_is_not_near_match() -> None:
    # Same NSN-edit-distance-1, but Hong Kong (+852) vs Singapore (+65) — the
    # region gate blocks it regardless of digit distance.
    assert phone_near_match("+6591234567", "+85291234568") is False


def test_phone_invalid_value_is_not_near_match() -> None:
    assert phone_near_match("not-a-phone", "+6591234567") is False


def test_phone_non_geographic_region_is_not_near_match() -> None:
    # +870 is a non-geographic (satellite) prefix — phonenumbers assigns it
    # region "001", which must be rejected rather than treated as a shared
    # region between two otherwise-unrelated numbers.
    assert phone_near_match("+8705112345678", "+8705112345679") is False


# --- email_near_match --------------------------------------------------------


def test_email_known_domain_typo_with_exact_local_is_near_match() -> None:
    assert email_near_match("john@gmial.com", "john@gmail.com") is True


def test_email_local_part_near_miss_with_same_domain_is_near_match() -> None:
    assert email_near_match("johnsmith@gmail.com", "jonhsmith@gmail.com") is True


def test_email_both_axes_fuzzy_is_not_near_match() -> None:
    assert email_near_match("johnsmith@gmial.com", "jonhsmith@gmail.com") is False


def test_email_unknown_domain_typo_is_not_near_match() -> None:
    assert email_near_match("john@foo.com", "john@fooo.com") is False


def test_email_short_local_part_is_not_near_match() -> None:
    assert email_near_match("abc@gmail.com", "abd@gmail.com") is False


def test_email_identical_addresses_are_not_near_match() -> None:
    assert email_near_match("john@gmail.com", "john@gmail.com") is False
