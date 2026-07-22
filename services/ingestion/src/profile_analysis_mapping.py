"""Strict mapping from allowlisted flat Neo4j rows to redacted snapshots."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from neo4j import Record

from src.models import QualityFlag, RecordType
from src.profile_analysis_snapshot import (
    AgeBand,
    CompletenessBand,
    DataQualityArea,
    ProfileAnalysisSnapshot,
    ProfileSignalsInput,
    ProfileSnapshotInput,
    RelationshipDirection,
    RelationshipSnapshotInput,
    SafeVehicleRelationship,
    SnapshotDataQualityInput,
    SnapshotOrderInput,
    SnapshotOrderItemInput,
    SnapshotSourceRecordInput,
    SnapshotVehicleInput,
    SourceTrustTier,
    build_redacted_profile_snapshot,
)
from src.profile_analysis_snapshot_values import CurrencyCode, SafeSnapshotLabel, SnapshotDate

type GraphScalar = str | int | float | bool | datetime | date | None
type GraphRow = Record | Mapping[str, GraphScalar]


class ProfileAnalysisMappingError(ValueError):
    """A graph value could not enter the reviewed snapshot boundary safely."""


@dataclass(slots=True)
class _OrderAccumulator:
    order_id: str
    order_date: SnapshotDate | None
    total: float | None
    currency: CurrencyCode | None
    merchant: SafeSnapshotLabel | None
    items: list[SnapshotOrderItemInput]

    def build(self) -> SnapshotOrderInput:
        return SnapshotOrderInput(
            order_id=self.order_id,
            order_date=self.order_date,
            total=self.total,
            currency=self.currency,
            merchant=self.merchant,
            items=tuple(self.items),
        )


def build_profile_analysis_snapshot(
    person_id: str,
    rows: Iterable[GraphRow],
) -> ProfileAnalysisSnapshot:
    """Convert explicit query columns into Task 2's reviewed input types."""
    profile = ProfileSignalsInput()
    sources: list[SnapshotSourceRecordInput] = []
    orders: dict[str, _OrderAccumulator] = {}
    vehicles: list[SnapshotVehicleInput] = []
    relationships: list[RelationshipSnapshotInput] = []
    query_data_quality = SnapshotDataQualityInput()
    try:
        for row in rows:
            row_kind = required_str(row, "row_kind")
            if row_kind == "profile":
                profile = _map_profile(row)
            elif row_kind == "source":
                sources.append(_map_source(row))
            elif row_kind == "order":
                _map_order_row(row, orders)
            elif row_kind == "vehicle":
                vehicles.append(_map_vehicle(row))
            elif row_kind == "relationship":
                relationships.append(_map_relationship(row))
            elif row_kind == "counts":
                query_data_quality = _map_omitted_counts(row)
            else:
                raise ValueError("unknown snapshot row kind")
        source = ProfileSnapshotInput(
            person_id=person_id,
            profile=profile,
            source_records=tuple(sources),
            orders=tuple(order.build() for order in orders.values()),
            vehicles=tuple(vehicles),
            relationships=tuple(relationships),
            data_quality=_data_quality(
                sources,
                orders,
                vehicles,
                relationships,
                query_data_quality,
            ),
        )
        return build_redacted_profile_snapshot(source)
    except (TypeError, ValueError):
        raise ProfileAnalysisMappingError("invalid safe profile analysis snapshot data") from None


def _map_profile(row: GraphRow) -> ProfileSignalsInput:
    age_band_value = _optional_str(row, "age_band")
    return ProfileSignalsInput(
        age_band=AgeBand(age_band_value) if age_band_value is not None else None,
        completeness_band=CompletenessBand(required_str(row, "completeness_band")),
        completeness_score=_optional_float(row, "completeness_score"),
    )


def _map_source(row: GraphRow) -> SnapshotSourceRecordInput:
    return SnapshotSourceRecordInput(
        source_record_id=required_str(row, "internal_id"),
        record_type=RecordType(required_str(row, "record_type")),
        source_category=SafeSnapshotLabel(required_str(row, "source_category")),
        observed_date=_optional_date(row, "observed_date"),
        quality_flag=_optional_quality_flag(row, "quality_flag"),
        trust_tier=_optional_trust_tier(row, "trust_tier"),
        confidence=_optional_float(row, "confidence"),
    )


def _map_order_row(
    row: GraphRow,
    orders: dict[str, _OrderAccumulator],
) -> None:
    order_id = required_str(row, "internal_id")
    order_date = _optional_date(row, "order_date")
    total = _optional_float(row, "total")
    currency = _optional_currency(row, "currency")
    merchant = _optional_label(row, "merchant")
    order = orders.get(order_id)
    if order is None:
        order = _OrderAccumulator(
            order_id=order_id,
            order_date=order_date,
            total=total,
            currency=currency,
            merchant=merchant,
            items=[],
        )
        orders[order_id] = order
    elif (order.order_date, order.total, order.currency, order.merchant) != (
        order_date,
        total,
        currency,
        merchant,
    ):
        raise ValueError("conflicting duplicate order metadata")
    product = _optional_label(row, "product")
    category = _optional_label(row, "category")
    if product is not None or category is not None:
        item = SnapshotOrderItemInput(product=product, category=category)
        if item not in order.items:
            order.items.append(item)


def _map_vehicle(row: GraphRow) -> SnapshotVehicleInput:
    return SnapshotVehicleInput(
        vehicle_id=required_str(row, "internal_id"),
        product=_optional_label(row, "product"),
        manufacturer=_optional_label(row, "manufacturer"),
        model=_optional_label(row, "model"),
        relationship_category=SafeVehicleRelationship(required_str(row, "relationship_category")),
    )


def _map_relationship(row: GraphRow) -> RelationshipSnapshotInput:
    return RelationshipSnapshotInput(
        relationship_id=required_str(row, "internal_id"),
        related_person_id=required_str(row, "parent_internal_id"),
        category=SafeSnapshotLabel(required_str(row, "relationship_category")),
        direction=RelationshipDirection(required_str(row, "direction")),
        event_date=_optional_date(row, "event_date"),
    )


def _map_omitted_counts(row: GraphRow) -> SnapshotDataQualityInput:
    return SnapshotDataQualityInput(
        omitted_sources=required_int(row, "omitted_sources"),
        omitted_orders=required_int(row, "omitted_orders"),
        omitted_order_items=required_int(row, "omitted_order_items"),
        omitted_vehicles=required_int(row, "omitted_vehicles"),
        omitted_relationships=required_int(row, "omitted_relationships"),
    )


def _data_quality(
    sources: Sequence[SnapshotSourceRecordInput],
    orders: Mapping[str, _OrderAccumulator],
    vehicles: Sequence[SnapshotVehicleInput],
    relationships: Sequence[RelationshipSnapshotInput],
    query_data_quality: SnapshotDataQualityInput,
) -> SnapshotDataQualityInput:
    gaps: list[DataQualityArea] = []
    if not sources or any(
        item.quality_flag is None or item.trust_tier is None or item.confidence is None
        for item in sources
    ):
        gaps.append(DataQualityArea.SOURCE_RECORDS)
    if not orders:
        gaps.append(DataQualityArea.ORDERS)
    if not vehicles:
        gaps.append(DataQualityArea.VEHICLES)
    if not relationships:
        gaps.append(DataQualityArea.RELATIONSHIPS)
    return SnapshotDataQualityInput(
        data_gaps=tuple(gaps),
        omitted_sources=query_data_quality.omitted_sources,
        omitted_orders=query_data_quality.omitted_orders,
        omitted_order_items=query_data_quality.omitted_order_items,
        omitted_vehicles=query_data_quality.omitted_vehicles,
        omitted_relationships=query_data_quality.omitted_relationships,
    )


def required_str(row: GraphRow | None, key: str) -> str:
    if row is None:
        raise ValueError("missing graph row")
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("invalid graph string")
    return value


def required_int(row: GraphRow, key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid graph integer")
    return value


def required_bool(row: GraphRow | None, key: str) -> bool:
    if row is None:
        raise ValueError("missing graph row")
    value = row.get(key)
    if not isinstance(value, bool):
        raise ValueError("invalid graph boolean")
    return value


def optional_bool(row: GraphRow, key: str) -> bool | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError("invalid optional graph boolean")
    return value


def _optional_str(row: GraphRow, key: str) -> str | None:
    value = row.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid optional graph string")
    return value


def _optional_float(row: GraphRow, key: str) -> float | None:
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid graph number")
    return float(value)


def _optional_label(row: GraphRow, key: str) -> SafeSnapshotLabel | None:
    value = _optional_str(row, key)
    return SafeSnapshotLabel(value) if value is not None else None


def _optional_currency(row: GraphRow, key: str) -> CurrencyCode | None:
    value = _optional_str(row, key)
    return CurrencyCode(value) if value is not None else None


def _optional_quality_flag(row: GraphRow, key: str) -> QualityFlag | None:
    value = _optional_str(row, key)
    return QualityFlag(value) if value is not None else None


def _optional_trust_tier(row: GraphRow, key: str) -> SourceTrustTier | None:
    value = _optional_str(row, key)
    return SourceTrustTier(value) if value is not None else None


def _optional_date(row: GraphRow, key: str) -> SnapshotDate | None:
    value = _optional_str(row, key)
    return SnapshotDate(value) if value is not None else None
