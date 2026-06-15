"""Email normalization, including Gmail dot/plus-addressing canonicalization."""

from __future__ import annotations

from src.models import QualityFlag
from src.normalizers.email import normalize_email


def test_lowercases_and_strips_whitespace() -> None:
    assert normalize_email("  John.Tan@Example.com  ") == (
        "john.tan@example.com",
        QualityFlag.VALID,
    )


def test_gmail_dot_in_local_part_is_removed() -> None:
    assert normalize_email("john.tan@gmail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_gmail_plus_tag_is_stripped() -> None:
    assert normalize_email("johntan+promo@gmail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_gmail_dot_and_plus_tag_combined() -> None:
    assert normalize_email("John.Tan+promo@Gmail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_googlemail_domain_folds_to_gmail() -> None:
    assert normalize_email("john.tan@googlemail.com") == ("johntan@gmail.com", QualityFlag.VALID)


def test_non_gmail_domain_is_unaffected() -> None:
    assert normalize_email("john.tan+promo@hotmail.com") == (
        "john.tan+promo@hotmail.com",
        QualityFlag.VALID,
    )


def test_placeholder_still_rejected() -> None:
    assert normalize_email("test@test.com") == (None, QualityFlag.PLACEHOLDER_VALUE)


def test_invalid_format_still_rejected() -> None:
    assert normalize_email("not-an-email") == (None, QualityFlag.INVALID_FORMAT)
