"""Extract MachineUnit observations from connector payloads."""

from __future__ import annotations

from src.machine_units import MachineUnitObservation, valid_machine_unit_observation
from src.models import JsonValue, QualityFlag


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _product_name(line: dict[str, JsonValue]) -> str | None:
    product = line.get("product")
    if isinstance(product, dict):
        display = product.get("display_name")
        name = product.get("name")
        return _str_or_none(display) or _str_or_none(name)
    return None


def observations_from_sales_lines(
    *,
    source_system_key: str,
    source_record_id: str,
    observed_at: str | None,
    lines: list[JsonValue],
) -> list[MachineUnitObservation]:
    observations: list[MachineUnitObservation] = []
    for line in lines:
        if not isinstance(line, dict):
            continue
        metadata = line.get("metadata")
        if not isinstance(metadata, dict):
            continue
        observation = MachineUnitObservation(
            lta_tag=_str_or_none(metadata.get("lta_tag")),
            serial_number=_str_or_none(metadata.get("serial_no"))
            or _str_or_none(metadata.get("serialnumber")),
            machine_product=_product_name(line),
            unit_label=_str_or_none(metadata.get("unit")),
            source_kind="sales",
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            observed_at=observed_at,
            confidence=1.0,
            quality_flag=QualityFlag.VALID,
            raw_context=_str_or_none(line.get("source_line_item_id")),
        )
        if valid_machine_unit_observation(observation):
            observations.append(observation)
    return observations


def observations_from_chat_inquiries(
    *,
    source_system_key: str,
    source_record_id: str,
    observed_at: str | None,
    inquiries: list[JsonValue],
) -> list[MachineUnitObservation]:
    observations: list[MachineUnitObservation] = []
    for inquiry in inquiries:
        if not isinstance(inquiry, dict):
            continue
        observation = MachineUnitObservation(
            lta_tag=_str_or_none(inquiry.get("lta_tag")),
            serial_number=_str_or_none(inquiry.get("serial_number")),
            machine_product=_str_or_none(inquiry.get("machine_product")),
            unit_label=_str_or_none(inquiry.get("unit")),
            source_kind="chat_inquiry",
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            observed_at=observed_at,
            confidence=0.6,
            quality_flag=QualityFlag.PARTIAL_PARSE,
            raw_context=_str_or_none(inquiry.get("notes")),
        )
        if valid_machine_unit_observation(observation):
            observations.append(observation)
    return observations
