from __future__ import annotations

from src.connectors.chat_helpers import ExtractionResult
from src.exclusions import (
    ExclusionContext,
    filter_extraction,
    is_excluded_email,
    is_excluded_name,
    is_excluded_phone,
)


def test_phone_exclusion_normalizes_singapore_numbers() -> None:
    context = ExclusionContext(phones=frozenset({"+6568505434"}))

    assert is_excluded_phone("6850 5434", context)


def test_filter_extraction_removes_excluded_person_and_keeps_customer() -> None:
    extraction = ExtractionResult(
        confidence=0.9,
        summary="customer spoke with agent",
        persons=[
            {"name": "Agent One", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": "customer@example.com"},
        ],
        transactions=[],
    )
    context = ExclusionContext(phones=frozenset({"+6568505434"}), names=frozenset({"agent one"}))

    filtered = filter_extraction(extraction, context)

    assert filtered is not None
    assert filtered["persons"] == [
        {"name": "Customer One", "phone": "+6588889999", "email": "customer@example.com"}
    ]


def test_filter_extraction_returns_none_when_all_people_excluded() -> None:
    extraction = ExtractionResult(
        confidence=0.9,
        summary="agent only",
        persons=[{"name": "Agent One", "phone": "+6568505434", "email": None}],
        transactions=[],
    )
    context = ExclusionContext(phones=frozenset({"+6568505434"}), names=frozenset({"agent one"}))

    assert filter_extraction(extraction, context) is None


def test_email_and_name_checks_are_exact_after_normalization() -> None:
    context = ExclusionContext(
        emails=frozenset({"staff@example.com"}),
        names=frozenset({"staff member"}),
    )

    assert is_excluded_email(" Staff@Example.com ", context)
    assert is_excluded_name("  Staff   Member ", context)
    assert not is_excluded_name("Staff Member Jr", context)
