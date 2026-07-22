"""Build and validate typed, redacted snapshots for Person profile analysis."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from pydantic import TypeAdapter, ValidationError

from src.models import JsonValue
from src.profile_analysis_snapshot_types import (
    SNAPSHOT_SCHEMA_VERSION,
    AgeBand,
    CompletenessBand,
    DataQualityArea,
    OrderEvidence,
    OrderItemEvidence,
    ProfileAnalysisSnapshot,
    ProfileSignalsInput,
    ProfileSnapshotInput,
    RelationshipDirection,
    RelationshipEvidence,
    RelationshipSnapshotInput,
    SafeVehicleRelationship,
    SnapshotDataQualityInput,
    SnapshotOrderInput,
    SnapshotOrderItemInput,
    SnapshotSourceRecordInput,
    SnapshotVehicleInput,
    SourceEvidence,
    SourceTrustTier,
    VehicleEvidence,
)
from src.profile_analysis_snapshot_values import CurrencyCode, SafeSnapshotLabel, SnapshotDate

__all__ = [
    "SNAPSHOT_SCHEMA_VERSION",
    "AgeBand",
    "CompletenessBand",
    "CurrencyCode",
    "DataQualityArea",
    "KnownSensitiveValue",
    "ProfileAnalysisPrivacyError",
    "ProfileAnalysisSnapshot",
    "ProfileSignalsInput",
    "ProfileSnapshotInput",
    "RelationshipDirection",
    "RelationshipSnapshotInput",
    "SafeSnapshotLabel",
    "SafeVehicleRelationship",
    "SnapshotDataQualityInput",
    "SnapshotDate",
    "SnapshotOrderInput",
    "SnapshotOrderItemInput",
    "SnapshotSourceRecordInput",
    "SnapshotVehicleInput",
    "SourceTrustTier",
    "build_redacted_profile_snapshot",
    "canonical_snapshot_json",
    "compact_sensitive_text",
    "snapshot_fingerprint",
    "validate_profile_analysis_boundary",
    "finite_sensitive_decimal",
    "normalize_sensitive_text",
]


class ProfileAnalysisPrivacyError(ValueError):
    """Safe failure raised when serialized data violates the LLM boundary."""


_MAX_SOURCES = 20
_MAX_ORDERS = 8
_MAX_ITEMS_PER_ORDER = 5
_MAX_VEHICLES = 10
_MAX_RELATIONSHIPS = 20


def build_redacted_profile_snapshot(source: ProfileSnapshotInput) -> ProfileAnalysisSnapshot:
    """Build an immutable snapshot without copying direct or raw input fields."""
    sorted_sources = sorted(source.source_records, key=_source_sort_key)
    sorted_orders = sorted(source.orders, key=_order_sort_key)
    sorted_vehicles = sorted(source.vehicles, key=_vehicle_sort_key)
    sorted_relationships = sorted(
        source.relationships,
        key=lambda item: (
            item.related_person_id,
            item.category.value,
            item.direction.value,
            _date_value(item.event_date),
            item.relationship_id,
        ),
    )
    sources = sorted_sources[-_MAX_SOURCES:]
    orders = sorted_orders[-_MAX_ORDERS:]
    vehicles = sorted_vehicles[:_MAX_VEHICLES]
    relationships = sorted_relationships[-_MAX_RELATIONSHIPS:]
    aliases = _relationship_aliases(tuple(relationships))
    truncated_areas = tuple(
        area
        for truncated, area in (
            (len(sorted_sources) > len(sources), DataQualityArea.SOURCE_RECORDS),
            (len(sorted_orders) > len(orders), DataQualityArea.ORDERS),
            (len(sorted_vehicles) > len(vehicles), DataQualityArea.VEHICLES),
            (len(sorted_relationships) > len(relationships), DataQualityArea.RELATIONSHIPS),
        )
        if truncated
    )
    return ProfileAnalysisSnapshot(
        profile=source.profile,
        sources=tuple(_safe_source(index, item) for index, item in enumerate(sources, 1)),
        orders=tuple(_safe_order(index, item) for index, item in enumerate(orders, 1)),
        vehicles=tuple(_safe_vehicle(index, item) for index, item in enumerate(vehicles, 1)),
        relationships=tuple(
            RelationshipEvidence(
                evidence_ref=f"relationship-{index}",
                contact_alias=aliases[item.related_person_id],
                category=item.category,
                direction=item.direction,
                event_date=item.event_date,
            )
            for index, item in enumerate(relationships, 1)
        ),
        data_quality=SnapshotDataQualityInput(
            data_gaps=_sorted_areas(source.data_quality.data_gaps + truncated_areas),
            conflicts=_sorted_areas(source.data_quality.conflicts),
            stale_areas=_sorted_areas(source.data_quality.stale_areas),
            omitted_sources=source.data_quality.omitted_sources
            + len(sorted_sources)
            - len(sources),
            omitted_orders=source.data_quality.omitted_orders + len(sorted_orders) - len(orders),
            omitted_order_items=source.data_quality.omitted_order_items
            + sum(len(order.items) for order in sorted_orders)
            - sum(min(len(order.items), _MAX_ITEMS_PER_ORDER) for order in orders),
            omitted_vehicles=source.data_quality.omitted_vehicles
            + len(sorted_vehicles)
            - len(vehicles),
            omitted_relationships=source.data_quality.omitted_relationships
            + len(sorted_relationships)
            - len(relationships),
        ),
    )


def canonical_snapshot_json(snapshot: ProfileAnalysisSnapshot) -> str:
    """Serialize a redacted snapshot canonically without persisting it."""
    return json.dumps(
        snapshot.to_payload(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def snapshot_fingerprint(snapshot: ProfileAnalysisSnapshot) -> str:
    """Return the SHA-256 fingerprint of the canonical redacted snapshot."""
    canonical = canonical_snapshot_json(snapshot).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_FORBIDDEN_KEY_FRAGMENTS = frozenset(
    {
        "name",
        "nric",
        "governmentid",
        "govtid",
        "phone",
        "email",
        "dob",
        "dateofbirth",
        "birthdate",
        "birthday",
        "address",
        "postal",
        "postcode",
        "zipcode",
        "unit",
        "personid",
        "sourcerecordid",
        "sourcerecordpk",
        "sourceid",
        "internalid",
        "graphid",
        "elementid",
        "neo4j",
        "nodeid",
        "edgeid",
        "orderid",
        "vehicleid",
        "relationshipid",
        "serial",
        "lta",
        "raw",
        "transcript",
        "conversationtext",
        "messagestext",
        "normalizedvalue",
    }
)

type KnownSensitiveValue = str | int | float | Decimal

_MIN_SUBSTRING_SENSITIVE_LENGTH = 4


def validate_profile_analysis_boundary(
    serialized_data: str,
    known_sensitive_values: Sequence[KnownSensitiveValue] = (),
) -> None:
    """Reject dangerous keys or supplied sensitive values without echoing them."""
    try:
        value = _JSON_ADAPTER.validate_json(serialized_data)
    except ValidationError:
        raise ProfileAnalysisPrivacyError("profile analysis data is not valid JSON") from None
    (
        numeric_sensitive,
        exact_text_sensitive,
        substring_text_sensitive,
        compact_text_sensitive,
    ) = _normalize_sensitive_values(known_sensitive_values)
    _validate_boundary_value(
        value,
        numeric_sensitive,
        exact_text_sensitive,
        substring_text_sensitive,
        compact_text_sensitive,
    )


def _validate_boundary_value(
    value: JsonValue,
    numeric_sensitive: frozenset[Decimal],
    exact_text_sensitive: frozenset[str],
    substring_text_sensitive: tuple[str, ...],
    compact_text_sensitive: tuple[str, ...],
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = "".join(
                character for character in normalize_sensitive_text(key) if character.isalnum()
            )
            if any(fragment in normalized_key for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise ProfileAnalysisPrivacyError("profile analysis data contains a forbidden key")
            _validate_boundary_value(
                child,
                numeric_sensitive,
                exact_text_sensitive,
                substring_text_sensitive,
                compact_text_sensitive,
            )
        return
    if isinstance(value, list):
        for child in value:
            _validate_boundary_value(
                child,
                numeric_sensitive,
                exact_text_sensitive,
                substring_text_sensitive,
                compact_text_sensitive,
            )
        return
    if isinstance(value, str):
        normalized_value = normalize_sensitive_text(value)
        compact_value = compact_sensitive_text(value)
        numeric_value = finite_sensitive_decimal(normalized_value)
        if (
            numeric_value is not None
            and numeric_value in numeric_sensitive
            or normalized_value in exact_text_sensitive
            or any(item in normalized_value for item in substring_text_sensitive)
            or any(item in compact_value for item in compact_text_sensitive)
        ):
            _raise_sensitive_value_error()
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        numeric_value = finite_sensitive_decimal(str(value))
        if numeric_value is not None and numeric_value in numeric_sensitive:
            _raise_sensitive_value_error()


def _normalize_sensitive_values(
    values: Sequence[KnownSensitiveValue],
) -> tuple[frozenset[Decimal], frozenset[str], tuple[str, ...], tuple[str, ...]]:
    numeric_values: set[Decimal] = set()
    exact_text_values: set[str] = set()
    substring_text_values: list[str] = []
    compact_text_values: list[str] = []
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float, Decimal)):
            numeric_value = finite_sensitive_decimal(str(value))
            if numeric_value is not None:
                numeric_values.add(numeric_value)
            compact_value = compact_sensitive_text(str(value))
            if len(compact_value) >= _MIN_SUBSTRING_SENSITIVE_LENGTH:
                compact_text_values.append(compact_value)
            continue
        normalized_value = normalize_sensitive_text(value)
        if not normalized_value:
            continue
        numeric_value = finite_sensitive_decimal(normalized_value)
        if numeric_value is not None:
            numeric_values.add(numeric_value)
        if len(normalized_value) < _MIN_SUBSTRING_SENSITIVE_LENGTH:
            exact_text_values.add(normalized_value)
        else:
            substring_text_values.append(normalized_value)
        compact_value = compact_sensitive_text(normalized_value)
        if len(compact_value) >= _MIN_SUBSTRING_SENSITIVE_LENGTH:
            compact_text_values.append(compact_value)
    return (
        frozenset(numeric_values),
        frozenset(exact_text_values),
        tuple(substring_text_values),
        tuple(compact_text_values),
    )


def normalize_sensitive_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def compact_sensitive_text(value: str) -> str:
    """Normalize formatting differences in direct identifiers and addresses."""
    return "".join(
        character for character in normalize_sensitive_text(value) if character.isalnum()
    )


def finite_sensitive_decimal(value: str) -> Decimal | None:
    try:
        numeric = Decimal(value)
    except InvalidOperation:
        return None
    return numeric if numeric.is_finite() else None


def _raise_sensitive_value_error() -> None:
    raise ProfileAnalysisPrivacyError("profile analysis data contains a known sensitive value")


def _relationship_aliases(
    relationships: tuple[RelationshipSnapshotInput, ...],
) -> dict[str, str]:
    related_ids = sorted({item.related_person_id for item in relationships})
    return {related_id: _contact_alias(index) for index, related_id in enumerate(related_ids)}


def _contact_alias(index: int) -> str:
    letters: list[str] = []
    current = index
    while True:
        current, remainder = divmod(current, 26)
        letters.append(chr(ord("A") + remainder))
        if current == 0:
            break
        current -= 1
    return f"Contact {''.join(reversed(letters))}"


def _sorted_areas(areas: tuple[DataQualityArea, ...]) -> tuple[DataQualityArea, ...]:
    return tuple(sorted(set(areas), key=lambda item: item.value))


def _source_sort_key(item: SnapshotSourceRecordInput) -> tuple[str, ...]:
    return (
        _date_value(item.observed_date),
        item.record_type.value,
        item.source_category.value,
        item.quality_flag.value if item.quality_flag is not None else "",
        item.trust_tier.value if item.trust_tier is not None else "",
        str(item.confidence),
        item.source_record_id,
    )


def _order_sort_key(item: SnapshotOrderInput) -> tuple[str, ...]:
    items = sorted(item.items, key=_order_item_sort_key)
    item_key = "\x1f".join(
        f"{_label_value(child.product)}\x1e{_label_value(child.category)}" for child in items
    )
    return (
        _date_value(item.order_date),
        str(item.total),
        _currency_value(item.currency),
        _label_value(item.merchant),
        item_key,
        item.order_id,
    )


def _vehicle_sort_key(item: SnapshotVehicleInput) -> tuple[str, ...]:
    return (
        _label_value(item.product),
        _label_value(item.manufacturer),
        _label_value(item.model),
        item.relationship_category.value,
        item.vehicle_id,
    )


def _safe_source(index: int, item: SnapshotSourceRecordInput) -> SourceEvidence:
    return SourceEvidence(
        evidence_ref=f"source-{index}",
        record_type=item.record_type,
        source_category=item.source_category,
        observed_date=item.observed_date,
        quality_flag=item.quality_flag,
        trust_tier=item.trust_tier,
        confidence=item.confidence,
    )


def _safe_order(index: int, item: SnapshotOrderInput) -> OrderEvidence:
    sorted_items = sorted(
        item.items,
        key=_order_item_sort_key,
    )[:_MAX_ITEMS_PER_ORDER]
    return OrderEvidence(
        evidence_ref=f"order-{index}",
        order_date=item.order_date,
        total=item.total,
        currency=item.currency,
        merchant=item.merchant,
        items=tuple(
            OrderItemEvidence(product=child.product, category=child.category)
            for child in sorted_items
        ),
    )


def _safe_vehicle(index: int, item: SnapshotVehicleInput) -> VehicleEvidence:
    return VehicleEvidence(
        evidence_ref=f"vehicle-{index}",
        product=item.product,
        manufacturer=item.manufacturer,
        model=item.model,
        relationship_category=item.relationship_category,
    )


def _order_item_sort_key(item: SnapshotOrderItemInput) -> tuple[str, str]:
    return (_label_value(item.product), _label_value(item.category))


def _label_value(label: SafeSnapshotLabel | None) -> str:
    return label.value if label is not None else ""


def _currency_value(currency: CurrencyCode | None) -> str:
    return currency.value if currency is not None else ""


def _date_value(snapshot_date: SnapshotDate | None) -> str:
    return snapshot_date.value if snapshot_date is not None else ""
