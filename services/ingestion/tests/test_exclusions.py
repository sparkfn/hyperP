from __future__ import annotations

from src.connectors.chat_helpers import ExtractedPerson, ExtractedTransaction, ExtractionResult
from src.exclusion_config import ExclusionFile
from src.exclusions import (
    ExclusionContext,
    build_exclusion_context,
    filter_extraction,
    is_excluded_email,
    is_excluded_machine_unit_observation,
    is_excluded_name,
    is_excluded_phone,
)
from src.machine_units import MachineUnitObservation
from src.models import QualityFlag


def _extraction_result(
    persons: list[ExtractedPerson],
    transactions: list[ExtractedTransaction] | None = None,
) -> ExtractionResult:
    result: ExtractionResult = {
        "confidence": 0.9,
        "summary": "customer spoke with agent",
        "persons": persons,
        "possible_persons": [],
        "transactions": [] if transactions is None else transactions,
        "chat_members": [{"name": "Agent One", "phone": "+6568505434", "role": "agent"}],
        "inquiries": [{"machine_product": "loader", "unit": "A1"}],
        "strong_identifiers": [],
        "weak_identifiers": [],
        "customer_sentiment": "positive",
    }
    return result


def test_build_exclusion_context_merges_env_and_file_values() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=["+6581111111"],
        company_email_addresses=["env@example.com"],
        internal_person_names=["Env Person"],
        file_exclusions=ExclusionFile(
            phones=["+6582222222"],
            emails=["file@example.com"],
            email_domains=["Ada.Asia"],
            names=["File Person"],
            source_ids=["staff-1"],
        ),
    )

    assert "+6581111111" in context.phones
    assert "+6582222222" in context.phones
    assert "env@example.com" in context.emails
    assert "file@example.com" in context.emails
    assert "ada.asia" in context.email_domains
    assert "staff-1" in context.source_ids


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


def test_email_domain_exclusion_matches_domain_and_subdomains() -> None:
    context = ExclusionContext(email_domains=frozenset({"ada.asia"}))

    assert is_excluded_email("staff@ada.asia", context)
    assert is_excluded_email("staff@mail.ada.asia", context)
    assert not is_excluded_email("staff@notada.asia", context)


def test_email_domain_exclusion_normalizes_configured_domains() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(email_domains=["@SpeedZone.Asia", " mail.ADA.Asia "]),
    )

    assert is_excluded_email("person@speedzone.asia", context)
    assert is_excluded_email("person@x.mail.ada.asia", context)


def test_build_exclusion_context_normalizes_machine_unit_identifiers() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(
            machine_unit_identifiers=[
                {"machine_product": " Servicing   Labour ", "serial_number": "1186#1506"}
            ]
        ),
    )

    observation = MachineUnitObservation(
        source_kind="sales",
        source_system_key="speedzone_phppos:sales",
        source_record_id="sale-1",
        serial_number=" 1186#1506 ",
        machine_product="servicing labour",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )

    assert is_excluded_machine_unit_observation(observation, context)


def test_machine_unit_exclusion_requires_same_product_and_identifier() -> None:
    context = build_exclusion_context(
        company_mobile_numbers=[],
        company_email_addresses=[],
        internal_person_names=[],
        file_exclusions=ExclusionFile(
            machine_unit_identifiers=[
                {"machine_product": "Servicing Labour", "serial_number": "1186#1506"}
            ]
        ),
    )
    different_serial = MachineUnitObservation(
        source_kind="sales",
        source_system_key="speedzone_phppos:sales",
        source_record_id="sale-2",
        serial_number="1186#9999",
        machine_product="Servicing Labour",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )
    different_product = MachineUnitObservation(
        source_kind="sales",
        source_system_key="speedzone_phppos:sales",
        source_record_id="sale-3",
        serial_number="1186#1506",
        machine_product="Useful Product",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )

    assert not is_excluded_machine_unit_observation(different_serial, context)
    assert not is_excluded_machine_unit_observation(different_product, context)
