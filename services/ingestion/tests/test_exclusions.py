from __future__ import annotations

from src.connectors.chat_helpers import ExtractedPerson, ExtractedTransaction, ExtractionResult
from src.exclusions import (
    ExclusionContext,
    filter_extraction,
    is_excluded_email,
    is_excluded_name,
    is_excluded_phone,
)


def _extraction_result(
    persons: list[ExtractedPerson],
    transactions: list[ExtractedTransaction] | None = None,
) -> ExtractionResult:
    return ExtractionResult(
        confidence=0.9,
        summary="customer spoke with agent",
        persons=persons,
        transactions=[] if transactions is None else transactions,
        chat_members=[{"name": "Agent One", "phone": "+6568505434", "role": "agent"}],
        inquiries=[{"machine_product": "loader", "unit": "A1"}],
        customer_sentiment="positive",
    )


def test_phone_exclusion_normalizes_singapore_numbers() -> None:
    context = ExclusionContext(phones=frozenset({"+6568505434"}))

    assert is_excluded_phone("6850 5434", context)


def test_filter_extraction_removes_excluded_person_and_keeps_customer() -> None:
    extraction = _extraction_result(
        persons=[
            {"name": "Agent One", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": "customer@example.com"},
        ]
    )
    context = ExclusionContext(phones=frozenset({"+6568505434"}), names=frozenset({"agent one"}))

    filtered = filter_extraction(extraction, context)

    assert filtered is not None
    assert filtered["persons"] == [
        {"name": "Customer One", "phone": "+6588889999", "email": "customer@example.com"}
    ]


def test_filter_extraction_preserves_non_person_fields() -> None:
    transaction: ExtractedTransaction = {"order_id": "SO-1", "currency": "SGD"}
    extraction = _extraction_result(
        persons=[
            {"name": "Agent One", "phone": "+6568505434", "email": None},
            {"name": "Customer One", "phone": "+6588889999", "email": None},
        ],
        transactions=[transaction],
    )
    context = ExclusionContext(phones=frozenset({"+6568505434"}), names=frozenset({"agent one"}))

    filtered = filter_extraction(extraction, context)

    assert filtered is not None
    assert filtered["transactions"] == [transaction]
    assert filtered["chat_members"] == extraction["chat_members"]
    assert filtered["inquiries"] == extraction["inquiries"]
    assert filtered["customer_sentiment"] == "positive"


def test_filter_extraction_returns_none_when_all_people_excluded() -> None:
    extraction = _extraction_result(
        persons=[{"name": "Agent One", "phone": "+6568505434", "email": None}]
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
