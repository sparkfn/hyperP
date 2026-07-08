"""Vehicle observation types and normalization helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.models import QualityFlag

VehicleSourceKind = Literal["sales", "chat_inquiry", "explicit_ownership_claim"]

_PLACEHOLDERS: frozenset[str] = frozenset(
    {"", "-", "--", "N/A", "NA", "NONE", "NULL", "UNKNOWN", "NIL"}
)


@dataclass(frozen=True)
class VehicleObservation:
    """Normalized vehicle evidence extracted from a source record."""

    source_kind: VehicleSourceKind
    source_system_key: str
    source_record_id: str
    lta_tag: str | None = None
    serial_number: str | None = None
    product_sku: str | None = None
    product: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    unit_label: str | None = None
    observed_at: str | None = None
    confidence: float = 0.0
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


def normalize_vehicle_product(value: str | None) -> str | None:
    cleaned = _clean(value)
    if cleaned is None:
        return None
    normalized = " ".join(cleaned.split())
    return normalized if normalized and normalized not in _PLACEHOLDERS else None


def valid_vehicle_observation(observation: VehicleObservation) -> bool:
    has_sku = observation.product_sku is not None and observation.product_sku.strip() != ""
    has_identifier = (
        normalize_lta_tag(observation.lta_tag) is not None
        or normalize_serial_number(observation.serial_number) is not None
    )
    return has_sku and has_identifier


def valid_chat_vehicle_observation(observation: VehicleObservation) -> bool:
    """Validator for chat-inquiry observations: a product NAME (not a SKU) + an identifier.

    Chat inquiries carry a free-text product name from LLM extraction, not a
    source-internal SKU. Resolution of an existing Vehicle is by LTA tag (global)
    or by serial + product-name match, so the product name is the required key.
    """
    has_product = observation.product is not None and observation.product.strip() != ""
    has_identifier = (
        normalize_lta_tag(observation.lta_tag) is not None
        or normalize_serial_number(observation.serial_number) is not None
    )
    return has_product and has_identifier
