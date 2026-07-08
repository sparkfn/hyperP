"""Extract Vehicle observations from connector payloads."""

from __future__ import annotations

from src.models import JsonValue, QualityFlag
from src.normalizers.clean import str_or_none
from src.vehicle_categories import (
    base_source_key,
    category_is_vehicle,
    vehicle_category_allowlist,
)
from src.vehicles import (
    VehicleObservation,
    valid_chat_vehicle_observation,
    valid_vehicle_observation,
)

# Generic vehicle-type keywords used to classify free-text chat-inquiry product
# names for sources that have no per-source category allowlist (e.g. chat sources).
_CHAT_INQUIRY_VEHICLE_KEYWORDS: frozenset[str] = frozenset(
    {
        "BIKE",
        "BIKES",
        "BICYCLE",
        "BICYCLES",
        "E-BIKE",
        "EBIKE",
        "SCOOTER",
        "SCOOTERS",
        "MOTORBIKE",
        "MOTORBIKES",
        "MOTORCYCLE",
        "MOTORCYCLES",
        "WHEELCHAIR",
        "WHEELCHAIRS",
        "PMD",
        "MOBILITY",
    }
)


def _append_unique_product_part(parts: list[str], value: object) -> None:
    part = str_or_none(value)
    if part is not None and part not in parts:
        parts.append(part)


def _product_name(line: dict[str, JsonValue]) -> str | None:
    product = line.get("product")
    if isinstance(product, dict):
        parts: list[str] = []
        _append_unique_product_part(parts, product.get("display_name"))
        _append_unique_product_part(parts, product.get("name"))
        _append_unique_product_part(parts, product.get("variant"))
        attributes = product.get("attributes")
        if isinstance(attributes, dict):
            _append_unique_product_part(parts, attributes.get("model"))
        if parts:
            return " / ".join(parts)
    return None


def _chat_inquiry_is_vehicle(source_system_key: str, product: str | None) -> bool:
    """Heuristically classify a free-text chat-inquiry product as a vehicle.

    If the source has a per-source category allowlist, the product is treated as
    a vehicle when any allowlist category keyword appears in the (uppercased)
    product name. Sources without an allowlist (chat sources) fall back to a
    generic vehicle-keyword check.
    """
    if product is None:
        return False
    name = product.strip().upper()
    if not name:
        return False
    allow = vehicle_category_allowlist(source_system_key)
    if allow is not None:
        for category in allow:
            if category.upper() in name:
                return True
        return False
    return any(keyword in name for keyword in _CHAT_INQUIRY_VEHICLE_KEYWORDS)


def observations_from_sales_lines(
    *,
    source_system_key: str,
    source_record_id: str,
    observed_at: str | None,
    lines: list[JsonValue],
) -> list[VehicleObservation]:
    observations: list[VehicleObservation] = []
    base_key = base_source_key(source_system_key)
    for line in lines:
        if not isinstance(line, dict):
            continue
        product_raw = line.get("product")
        product: dict[str, JsonValue] = product_raw if isinstance(product_raw, dict) else {}
        category = product.get("category")
        if not category_is_vehicle(base_key, str_or_none(category)):
            continue
        metadata_raw = line.get("metadata")
        metadata: dict[str, JsonValue] = metadata_raw if isinstance(metadata_raw, dict) else {}
        serial_number = (
            str_or_none(metadata.get("serial_number"))
            or str_or_none(metadata.get("serial_no"))
            or str_or_none(metadata.get("serialnumber"))
        )
        lta_tag = str_or_none(metadata.get("lta_tag"))
        product_sku = str_or_none(product.get("sku")) or str_or_none(product.get("item_number"))
        manufacturer = str_or_none(product.get("manufacturer"))
        model = str_or_none(product.get("model"))
        unit_label = str_or_none(line.get("unit"))
        raw_context = str_or_none(line.get("source_line_item_id")) or str_or_none(
            line.get("source_line_id")
        )
        observation = VehicleObservation(
            lta_tag=lta_tag,
            serial_number=serial_number,
            product_sku=product_sku,
            product=_product_name(line),
            manufacturer=manufacturer,
            model=model,
            unit_label=unit_label,
            source_kind="sales",
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            observed_at=observed_at,
            confidence=1.0,
            quality_flag=QualityFlag.VALID,
            raw_context=raw_context,
        )
        if valid_vehicle_observation(observation):
            observations.append(observation)
    return observations


def observations_from_chat_inquiries(
    *,
    source_system_key: str,
    source_record_id: str,
    observed_at: str | None,
    inquiries: list[JsonValue],
) -> list[VehicleObservation]:
    observations: list[VehicleObservation] = []
    for inquiry in inquiries:
        if not isinstance(inquiry, dict):
            continue
        product = str_or_none(inquiry.get("vehicle_product"))
        if not _chat_inquiry_is_vehicle(source_system_key, product):
            continue
        observation = VehicleObservation(
            lta_tag=str_or_none(inquiry.get("lta_tag")),
            serial_number=str_or_none(inquiry.get("serial_number")),
            product_sku=str_or_none(inquiry.get("product_sku")),
            product=product,
            manufacturer=str_or_none(inquiry.get("manufacturer")),
            model=str_or_none(inquiry.get("model")),
            unit_label=str_or_none(inquiry.get("unit")),
            source_kind="chat_inquiry",
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            observed_at=observed_at,
            confidence=0.6,
            quality_flag=QualityFlag.PARTIAL_PARSE,
            raw_context=str_or_none(inquiry.get("notes")),
        )
        if valid_chat_vehicle_observation(observation):
            observations.append(observation)
    return observations
