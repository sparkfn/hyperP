"""The fixed reviewed dataset definition and serialized row-schema contract."""

from __future__ import annotations

from intelligence.datasets.models import ACTIVITY_PROVENANCE, DatasetRow


def definition() -> dict[str, object]:
    """Return the reviewed feature, label, leakage, and partial-history contract."""
    return {
        "definition": "crm-deal-state-v1",
        "feature_allowlist": [
            "category_id",
            "stage_id",
            "stage_semantic_id",
            "deal_age_seconds",
            "source_version_age_seconds",
            "archived_activity_count_lower_bound",
            "companion_call_count_lower_bound",
            "seconds_since_last_eligible_archived_activity",
        ],
        "identity": "deal_refs_identity_revisions_only_at_feature_cutoff",
        "provenance_fields": [
            "selected_identity_global_revision",
            "selected_identity_event_id",
            "horizon_source_record_pk",
            "horizon_source_record_id",
            "horizon_source_record_version",
            "horizon_observed_at",
            "horizon_available_at",
            "horizon_first_known_at",
            "horizon_source_effective_at",
            "horizon_version_age_seconds",
            "included_activity_join_corroboration",
        ],
        "label": "deal_state_at_horizon_v1:P=open,S=won,F=lost",
        "partial_activity_provenance": ACTIVITY_PROVENANCE,
        "point_in_time": "all required evidence must be known and effective by its cutoff",
        "version": "crm-deal-state-v1",
    }


def schema() -> dict[str, object]:
    """Return the fixed dataset row schema without a generic extensibility escape hatch."""
    return {
        "schema_version": "crm-deal-state-dataset-v1",
        "row_fields": sorted(DatasetRow.__dataclass_fields__),
        "timestamp_format": "UTC ISO-8601 Z",
        "unknown_activity_counts": "null with activity_missingness_reason",
    }
