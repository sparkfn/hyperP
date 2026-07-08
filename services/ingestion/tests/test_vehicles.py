from __future__ import annotations

from src.models import QualityFlag
from src.vehicles import (
    VehicleObservation,
    normalize_lta_tag,
    normalize_serial_number,
    normalize_vehicle_product,
    valid_chat_vehicle_observation,
    valid_vehicle_observation,
)


def test_normalize_lta_tag_uppercases_and_removes_separators() -> None:
    assert normalize_lta_tag(" lta-123 45 ") == "LTA12345"


def test_normalize_serial_number_preserves_meaningful_punctuation() -> None:
    assert normalize_serial_number(" sn-09/a ") == "SN-09/A"


def test_normalize_vehicle_product_uppercases_and_collapses_spaces() -> None:
    assert normalize_vehicle_product("  Scooter   X / variant 2  ") == "SCOOTER X / VARIANT 2"


def test_placeholder_values_are_rejected() -> None:
    assert normalize_lta_tag("n/a") is None
    assert normalize_serial_number("--") is None
    assert normalize_vehicle_product("unknown") is None


def test_observation_is_invalid_without_product_sku() -> None:
    obs = VehicleObservation(
        lta_tag=None,
        serial_number="SN-09",
        product_sku=None,
        product="Model A",
        unit_label="Unit 7",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_vehicle_observation(obs) is False


def test_observation_is_invalid_without_identifier_even_with_sku() -> None:
    obs = VehicleObservation(
        lta_tag=None,
        serial_number=None,
        product_sku="SKU-1",
        product="Model A",
        unit_label="Unit 7",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_vehicle_observation(obs) is False


def test_observation_is_valid_with_sku_and_one_identifier() -> None:
    obs = VehicleObservation(
        lta_tag=None,
        serial_number="SN-09",
        product_sku="SKU-1",
        product="Model A",
        unit_label="Unit 7",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        observed_at="2026-05-14T00:00:00",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
        raw_context="line-1",
    )

    assert valid_vehicle_observation(obs) is True


def test_observation_is_valid_with_lta_tag_and_empty_sku_string_is_invalid() -> None:
    obs = VehicleObservation(
        lta_tag="LTA123",
        serial_number=None,
        product_sku="   ",
        product="Model A",
        source_kind="sales",
        source_system_key="speedzone_phppos",
        source_record_id="sale-1",
        confidence=1.0,
        quality_flag=QualityFlag.VALID,
    )

    assert valid_vehicle_observation(obs) is False


def test_chat_observation_is_valid_with_product_name_and_identifier() -> None:
    """Chat validator keys on product NAME + identifier; no SKU required."""
    obs = VehicleObservation(
        lta_tag="LTA123",
        serial_number=None,
        product_sku=None,
        product="Honda scooter",
        source_kind="chat_inquiry",
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-1",
        confidence=0.6,
        quality_flag=QualityFlag.PARTIAL_PARSE,
    )

    assert valid_chat_vehicle_observation(obs) is True


def test_chat_observation_is_valid_with_serial_and_no_sku() -> None:
    obs = VehicleObservation(
        lta_tag=None,
        serial_number="SN-CHAT-1",
        product_sku=None,
        product="Honda scooter",
        source_kind="chat_inquiry",
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-2",
        confidence=0.6,
        quality_flag=QualityFlag.PARTIAL_PARSE,
    )

    assert valid_chat_vehicle_observation(obs) is True


def test_chat_observation_is_invalid_without_product_even_with_identifier() -> None:
    obs = VehicleObservation(
        lta_tag="LTA123",
        serial_number="SN-09",
        product_sku=None,
        product=None,
        source_kind="chat_inquiry",
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-3",
        confidence=0.6,
        quality_flag=QualityFlag.PARTIAL_PARSE,
    )

    assert valid_chat_vehicle_observation(obs) is False


def test_chat_observation_is_invalid_without_identifier_even_with_product() -> None:
    obs = VehicleObservation(
        lta_tag=None,
        serial_number=None,
        product_sku=None,
        product="Honda scooter",
        source_kind="chat_inquiry",
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-4",
        confidence=0.6,
        quality_flag=QualityFlag.PARTIAL_PARSE,
    )

    assert valid_chat_vehicle_observation(obs) is False


def test_chat_observation_is_invalid_with_blank_product_string() -> None:
    obs = VehicleObservation(
        lta_tag="LTA123",
        serial_number=None,
        product_sku=None,
        product="   ",
        source_kind="chat_inquiry",
        source_system_key="whatsapp_chat",
        source_record_id="whatsapp-chat-5",
        confidence=0.6,
        quality_flag=QualityFlag.PARTIAL_PARSE,
    )

    assert valid_chat_vehicle_observation(obs) is False