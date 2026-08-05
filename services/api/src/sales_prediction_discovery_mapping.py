"""Private parsing and allowlisted aggregate projections for issue #124."""

from __future__ import annotations

import math
from collections import defaultdict

from src.sales_prediction_discovery_deals import deal_aggregate_key
from src.sales_prediction_discovery_payload import (
    first_value,
    nested_object,
    parse_payload,
    payload_status,
)
from src.sales_prediction_discovery_taxonomy import (
    record_type_taxonomy,
    scoped_entity,
    source_identity_token,
    source_taxonomy,
    taxonomy,
)
from src.sales_prediction_discovery_temporal import availability_status, datetime_from_iso

type DiscoveryScalar = str | int | float | bool | None
type DiscoveryRow = dict[str, DiscoveryScalar]


def aggregate_source_coverage(
    rows: list[DiscoveryRow], entity_keys: tuple[str, ...]
) -> list[DiscoveryRow]:
    """Project source inventory rows through a fixed report allowlist."""
    fields = (
        "entity_key",
        "source_key",
        "record_type",
        "record_count",
        "missing_observed_at_count",
        "missing_ingested_at_count",
        "active_record_count",
        "superseded_record_count",
    )
    output: list[DiscoveryRow] = []
    for row in rows:
        projected = {field: _scalar(row.get(field)) for field in fields}
        projected["entity_key"] = scoped_entity(row.get("entity_key"), entity_keys)
        projected["source_key"] = source_taxonomy(row.get("source_key"))
        projected["record_type"] = record_type_taxonomy(row.get("record_type"))
        output.append(projected)
    return sorted(output, key=lambda row: tuple(str(row[field]) for field in fields))


def aggregate_deals(
    rows: list[DiscoveryRow],
    as_of_at: str,
    report_cutoff_at: str,
    entity_keys: tuple[str, ...],
    stage_catalog: dict[str, frozenset[str]],
) -> list[DiscoveryRow]:
    """Report CRM field coverage without treating final state as historical state."""
    cutoff = datetime_from_iso(as_of_at)
    report_cutoff = datetime_from_iso(report_cutoff_at)
    counts: defaultdict[tuple[DiscoveryScalar, ...], int] = defaultdict(int)
    for row in _latest_versions(rows):
        key = deal_aggregate_key(row, cutoff, report_cutoff, entity_keys, stage_catalog)
        counts[key] += 1
    return _count_rows(
        counts,
        (
            "entity_key",
            "source_key",
            "category_status",
            "stage_id",
            "historical_availability_status",
            "amount_state",
            "currency_status",
            "currency_code",
            "probability_state",
            "assignment_state",
            "person_linkage_cardinality",
            "person_linkage_basis",
            "contact_cardinality",
            "ambiguous_contacts",
            "snapshot_revision_availability",
            "generic_crm_history_child_count",
            "raw_payload_status",
        ),
        "logical_deal_count",
    )


def aggregate_history_capability(
    rows: list[DiscoveryRow], entity_keys: tuple[str, ...]
) -> list[DiscoveryRow]:
    """Report snapshots and generic activity separately from authoritative transitions."""
    groups: defaultdict[tuple[str, str], list[DiscoveryRow]] = defaultdict(list)
    for index, row in enumerate(rows):
        identity_key: tuple[str, str] = (
            source_identity_token(row.get("source_key"), index),
            _identifier_token(row.get("logical_record_id"), index),
        )
        groups[identity_key].append(row)
    counts: defaultdict[tuple[DiscoveryScalar, ...], int] = defaultdict(int)
    for versions in groups.values():
        latest = max(versions, key=_version_or_zero)
        stages = {_raw_stage_taxonomy(row) for row in versions}
        stages.discard("invalid_or_unknown")
        generic_history_count = max(
            _integer(row.get("crm_history_child_count")) or 0 for row in versions
        )
        aggregate_key: tuple[DiscoveryScalar, ...] = (
            scoped_entity(latest.get("entity_key"), entity_keys),
            _version_band(len(versions)),
            "yes" if len(stages) > 1 else "no",
            "present_generic_activity_only" if generic_history_count else "not_observed",
            "unavailable",
            "unreconstructable",
            _movement_status(versions, "entity_key"),
            _category_movement_status(versions),
        )
        counts[aggregate_key] += 1
    return _count_rows(
        counts,
        (
            "entity_key",
            "snapshot_version_count",
            "snapshot_detected_stage_difference",
            "crm_history_activity_coverage",
            "authoritative_stage_transition_coverage",
            "first_won_reconstructability",
            "entity_ownership_movement",
            "category_movement",
        ),
        "logical_deal_count",
    )


def aggregate_interactions(
    rows: list[DiscoveryRow],
    as_of_at: str,
    report_cutoff_at: str,
    entity_keys: tuple[str, ...],
) -> list[DiscoveryRow]:
    """Report optional message/call coverage; it never affects label capability."""
    cutoff = datetime_from_iso(as_of_at)
    report_cutoff = datetime_from_iso(report_cutoff_at)
    counts: defaultdict[tuple[DiscoveryScalar, ...], int] = defaultdict(int)
    for row in _latest_versions(rows):
        record_type = record_type_taxonomy(row.get("record_type"))
        payload = parse_payload(row)
        duration = _integer(first_value(payload, None, "duration_seconds"))
        key = (
            scoped_entity(row.get("entity_key"), entity_keys),
            record_type,
            availability_status(row, cutoff, report_cutoff),
            _confidence_status(row.get("extraction_confidence")),
            _duration_state(duration, record_type),
            payload_status(row),
        )
        counts[key] += 1
    return _count_rows(
        counts,
        (
            "entity_key",
            "record_type",
            "historical_availability_status",
            "confidence_status",
            "call_duration_status",
            "raw_payload_status",
        ),
        "record_count",
    )


def aggregate_late_arrival(
    rows: list[DiscoveryRow], entity_keys: tuple[str, ...]
) -> list[DiscoveryRow]:
    """Project aggregate arrival-risk query results through a fixed allowlist."""
    fields = (
        "entity_key",
        "record_type",
        "record_count",
        "negative_delay_count",
        "late_arrival_count",
        "max_delay_seconds",
    )
    output: list[DiscoveryRow] = []
    for row in rows:
        projected = {field: _scalar(row.get(field)) for field in fields}
        projected["entity_key"] = scoped_entity(row.get("entity_key"), entity_keys)
        projected["record_type"] = record_type_taxonomy(row.get("record_type"))
        output.append(projected)
    return sorted(output, key=lambda row: tuple(str(row[field]) for field in fields))


def capability_rows(entity_keys: tuple[str, ...]) -> list[DiscoveryRow]:
    """Describe known connector semantics without interpreting zero row counts."""
    declarations = (
        ("crm_deal_snapshot", "partial", "current_state_only_without_lifecycle_history"),
        ("authoritative_crm_stage_transition", "unavailable", "unavailable"),
        ("crm_history", "generic_crm_activity_only", "not_stage_transition_evidence"),
        ("person_deal_linkage", "observed_current_projection", "unavailable"),
    )
    return [
        {
            "entity_key": entity_key,
            "source_key": "bitrix_chat",
            "connector_contract": "bitrix_openlines_crm_v1",
            "capability": capability,
            "connector_schema_status": schema_status,
            "point_in_time_reconstruction_status": reconstruction_status,
        }
        for entity_key in entity_keys
        for capability, schema_status, reconstruction_status in declarations
    ]


def _latest_versions(rows: list[DiscoveryRow]) -> list[DiscoveryRow]:
    grouped: defaultdict[tuple[str, str, str], list[DiscoveryRow]] = defaultdict(list)
    for index, row in enumerate(rows):
        key = (
            source_identity_token(row.get("source_key"), index),
            source_identity_token(row.get("record_type"), index),
            _identifier_token(row.get("logical_record_id"), index),
        )
        grouped[key].append(row)
    return [max(group, key=_version_or_zero) for _, group in sorted(grouped.items())]


def _identifier_token(value: object, index: int) -> str:
    """Keep malformed identities isolated without emitting them into a report."""
    return value if isinstance(value, str) and value else f"missing-identity-{index}"


def _scalar(value: object) -> DiscoveryScalar:
    return value if value is None or isinstance(value, str | int | float | bool) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
        return number if math.isfinite(number) else None
    return None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _duration_state(value: int | None, record_type: str) -> str:
    if record_type != "call":
        return "not_applicable"
    if value is None or value < 0:
        return "missing_or_invalid"
    return "present"


def _confidence_status(value: object) -> str:
    number = _number(value)
    return "present" if number is not None and 0 <= number <= 1 else "missing_or_invalid"


def _version_band(value: int) -> str:
    return "1" if value == 1 else "2_plus" if value > 1 else "0"


def _movement_status(rows: list[DiscoveryRow], key: str) -> str:
    values = {taxonomy(row.get(key)) for row in rows}
    return "observed" if len(values) > 1 else "not_observed"


def _category_movement_status(rows: list[DiscoveryRow]) -> str:
    values = {_raw_category_taxonomy(row) for row in rows}
    return "observed" if len(values) > 1 else "not_observed"


def _version_or_zero(row: DiscoveryRow) -> int:
    version = _integer(row.get("source_record_version"))
    return version if version is not None and version > 0 else 0


def _raw_stage_taxonomy(row: DiscoveryRow) -> str:
    payload = parse_payload(row)
    return taxonomy(first_value(payload, nested_object(payload, "deal"), "stage_id", "STAGE_ID"))


def _raw_category_taxonomy(row: DiscoveryRow) -> str:
    payload = parse_payload(row)
    return taxonomy(
        first_value(payload, nested_object(payload, "deal"), "category_id", "CATEGORY_ID")
    )


def _count_rows(
    counts: dict[tuple[DiscoveryScalar, ...], int], columns: tuple[str, ...], count_column: str
) -> list[DiscoveryRow]:
    rows: list[DiscoveryRow] = []
    for key in sorted(counts, key=lambda values: tuple(str(value) for value in values)):
        row = dict(zip(columns, key, strict=True))
        row[count_column] = counts[key]
        rows.append(row)
    return rows
