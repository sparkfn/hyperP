"""Machine unit observation types and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.models import QualityFlag

MachineUnitSourceKind = Literal["sales", "chat_inquiry", "explicit_ownership_claim"]

_PLACEHOLDERS: frozenset[str] = frozenset(
    {"", "-", "--", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "NIL"}
)


@dataclass(frozen=True)
class MachineUnitObservation:
    """Normalized machine-unit evidence extracted from a source record."""

    source_kind: MachineUnitSourceKind
    source_system_key: str
    source_record_id: str
    lta_tag: str | None = None
    serial_number: str | None = None
    machine_product: str | None = None
    unit_label: str | None = None
    observed_at: str | None = None
    confidence: float | None = None
    quality_flag: QualityFlag = QualityFlag.VALID
    raw_context: str | None = None


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().upper()
    if cleaned in _PLACEHOLDERS:
        return None
    return cleaned


def normalize_lta_tag(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = "".join(ch for ch in cleaned if ch.isalnum())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None


def normalize_serial_number(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = " ".join(cleaned.split())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None


def normalize_machine_product(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = " ".join(cleaned.split())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None


def valid_machine_unit_observation(observation: MachineUnitObservation) -> bool:
    has_product = normalize_machine_product(observation.machine_product) is not None
    has_identifier = (
        normalize_lta_tag(observation.lta_tag) is not None
        or normalize_serial_number(observation.serial_number) is not None
    )
    return has_product and has_identifier
