"""Logical manifest, row, disposition, and count validation for datasets."""

from __future__ import annotations

from collections.abc import Mapping

from intelligence.datasets.models import DATASET_SCHEMA_VERSION, DatasetRow


def manifest_shape(value: Mapping[str, object]) -> None:
    expected = {
        "config",
        "content_digest",
        "dataset_id",
        "disposition_counts",
        "payload_inventory",
        "provenance",
        "request_digest",
        "row_counts",
        "row_count",
        "schema_version",
    }
    provenance = {
        "activity": {
            "bitrix_completeness_asserted": False,
            "completeness": "legacy_partial_snapshot",
            "source_population": "neo4j_existing_records",
        }
    }
    if set(value) != expected or value.get("provenance") != provenance:
        raise ValueError("dataset manifest schema or provenance is invalid")
    if value.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("dataset manifest schema is invalid")
    if not isinstance(value.get("content_digest"), str) or not isinstance(
        value.get("request_digest"), str
    ):
        raise ValueError("dataset manifest digest is invalid")


def verify_counts(manifest: Mapping[str, object], logical: Mapping[str, object]) -> None:
    rows, dispositions = _logical_lists(logical)
    row_count = manifest.get("row_count")
    if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count != len(rows):
        raise ValueError("dataset row count is invalid")
    if manifest.get("row_counts") != _counts(rows, "disposition"):
        raise ValueError("dataset row disposition totals are invalid")
    if manifest.get("disposition_counts") != _counts(dispositions, "primary_disposition"):
        raise ValueError("dataset source disposition totals are invalid")


def logical_schemas(logical: Mapping[str, object]) -> None:
    rows, dispositions = _logical_lists(logical)
    identities: set[tuple[str, str, str]] = set()
    for row in rows:
        _row_schema(row, identities)
    for disposition in dispositions:
        _disposition_schema(disposition)


def _logical_lists(
    logical: Mapping[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    rows, dispositions = logical.get("rows"), logical.get("dispositions")
    if not isinstance(rows, list) or not isinstance(dispositions, list):
        raise ValueError("dataset logical content is invalid")
    if not all(isinstance(item, dict) for item in rows + dispositions):
        raise ValueError("dataset logical content is invalid")
    return rows, dispositions


def _counts(values: list[dict[str, object]], key: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        item = value.get(key)
        if not isinstance(item, str):
            raise ValueError("dataset disposition is invalid")
        result[item] = result.get(item, 0) + 1
    return dict(sorted(result.items()))


def _row_schema(row: dict[str, object], identities: set[tuple[str, str, str]]) -> None:
    if set(row) != set(DatasetRow.__dataclass_fields__):
        raise ValueError("dataset row schema is invalid")
    required = ("source_system", "source_instance_id", "source_entity_id")
    if not all(_text(row.get(key)) for key in required):
        raise ValueError("dataset row values are invalid")
    if (
        row["source_system"] != "bitrix_chat"
        or row.get("activity_coverage") != "legacy_partial_snapshot"
    ):
        raise ValueError("dataset row values are invalid")
    if row.get("disposition") not in {"labeled", "censored", "ineligible", "unresolved"}:
        raise ValueError("dataset row disposition is invalid")
    identity = (
        str(row["source_system"]),
        str(row["source_instance_id"]),
        str(row["source_entity_id"]),
    )
    if identity in identities:
        raise ValueError("dataset rows duplicate a source entity")
    identities.add(identity)
    _optional_fields(row)
    _numeric_fields(row)
    _row_consistency(row)


def _optional_fields(row: Mapping[str, object]) -> None:
    fields = {
        "feature_source_record_pk",
        "horizon_source_record_pk",
        "person_id",
        "category_id",
        "stage_id",
        "stage_semantic_id",
        "activity_missingness_reason",
        "identity_reason",
        "feature_reason",
        "label",
        "label_reason",
    }
    for field in fields:
        value = row.get(field)
        if value is not None and not _text(value):
            raise ValueError("dataset row optional value is invalid")
    if row.get("label") not in {None, "open", "won", "lost"}:
        raise ValueError("dataset row label is invalid")


def _numeric_fields(row: Mapping[str, object]) -> None:
    fields = {
        "deal_age_seconds",
        "source_version_age_seconds",
        "horizon_version_age_seconds",
        "selected_identity_global_revision",
        "archived_activity_count_lower_bound",
        "companion_call_count_lower_bound",
        "seconds_since_last_eligible_archived_activity",
    }
    for field in fields:
        value = row.get(field)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError("dataset row numeric value is invalid")
    if row.get("included_activity_join_corroboration") not in {
        "none",
        "stored_parent_only",
        "stored_parent_graph_corroborated",
        "mixed",
    }:
        raise ValueError("dataset activity join provenance is invalid")


def _row_consistency(row: Mapping[str, object]) -> None:
    disposition = row.get("disposition")
    feature, person, label = (
        row.get("feature_source_record_pk"),
        row.get("person_id"),
        row.get("label"),
    )
    if disposition == "labeled" and (feature is None or person is None or label is None):
        raise ValueError("labeled dataset row is incomplete")
    if disposition == "ineligible" and feature is not None:
        raise ValueError("ineligible dataset row has a feature")
    if disposition == "unresolved" and (feature is None or person is not None):
        raise ValueError("unresolved dataset row is inconsistent")


def _disposition_schema(value: dict[str, object]) -> None:
    expected = {"kind", "primary_disposition", "reason_code", "source_record_pk"}
    if set(value) != expected or not _text(value.get("source_record_pk")):
        raise ValueError("dataset disposition schema is invalid")
    kind, primary, reason = (
        value.get("kind"),
        value.get("primary_disposition"),
        value.get("reason_code"),
    )
    allowed = {
        "activity": {
            "feature_included",
            "temporally_excluded",
            "join_excluded",
            "source_rejected",
            "source_quarantined",
        },
        "deal_version": {"selected_representative", "ambiguous_version", "superseded_or_excluded"},
    }
    if kind not in allowed or primary not in allowed[kind]:
        raise ValueError("dataset disposition value is invalid")
    requires_reason = (kind == "activity" and primary != "feature_included") or (
        kind == "deal_version" and primary == "ambiguous_version"
    )
    if requires_reason and not _text(reason):
        raise ValueError("dataset disposition reason is required")
    if not requires_reason and reason is not None:
        raise ValueError("dataset disposition reason is not permitted")
    if reason is not None and not _text(reason):
        raise ValueError("dataset disposition reason is invalid")


def _text(value: object) -> bool:
    return (
        isinstance(value, str) and 0 < len(value) <= 1024 and "/" not in value and "\\" not in value
    )
