"""JSON-backed ingestion exclusion configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict, cast

from src.models import JsonValue


class VehicleIdentifierExclusion(TypedDict, total=False):
    """Vehicle identifier exclusion values loaded from JSON."""

    vehicle_product: str
    lta_tag: str
    serial_number: str


VEHICLE_IDENTIFIER_KEYS: frozenset[str] = frozenset(
    {"vehicle_product", "lta_tag", "serial_number"}
)


@dataclass
class ExclusionFile:
    """Editable hard-exclusion values loaded from JSON."""

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    email_domains: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)
    vehicle_identifiers: list[VehicleIdentifierExclusion] = field(default_factory=list)


def _str_list(raw: JsonValue, *, path: Path) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
        values.append(value)
    return values


def _vehicle_identifier_list(
    raw: JsonValue, *, path: Path
) -> list[VehicleIdentifierExclusion]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    values: list[VehicleIdentifierExclusion] = []
    for value in raw:
        if not isinstance(value, dict):
            raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
        item = value
        if not set(item).issubset(VEHICLE_IDENTIFIER_KEYS):
            raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
        exclusion: VehicleIdentifierExclusion = {}
        if "vehicle_product" in item:
            vehicle_product = item["vehicle_product"]
            if not isinstance(vehicle_product, str):
                raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
            exclusion["vehicle_product"] = vehicle_product
        if "lta_tag" in item:
            lta_tag = item["lta_tag"]
            if not isinstance(lta_tag, str):
                raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
            exclusion["lta_tag"] = lta_tag
        if "serial_number" in item:
            serial_number = item["serial_number"]
            if not isinstance(serial_number, str):
                raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
            exclusion["serial_number"] = serial_number
        values.append(exclusion)
    return values


def load_exclusion_file(path_value: str) -> ExclusionFile:
    if not path_value.strip():
        return ExclusionFile()
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Ingestion exclusions file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    payload = cast(dict[str, JsonValue], raw)
    return ExclusionFile(
        phones=_str_list(payload.get("phones"), path=path),
        emails=_str_list(payload.get("emails"), path=path),
        email_domains=_str_list(payload.get("email_domains"), path=path),
        names=_str_list(payload.get("names"), path=path),
        source_ids=_str_list(payload.get("source_ids"), path=path),
        vehicle_identifiers=_vehicle_identifier_list(
            payload.get("vehicle_identifiers"), path=path
        ),
    )
