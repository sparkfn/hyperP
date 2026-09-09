"""Point-in-time identity selection, row construction, and deal dispositions."""

from __future__ import annotations

from intelligence.crm_deal_refs.models import DealReference, IdentityRevision
from intelligence.datasets.definition_join import ActivityEvidence
from intelligence.datasets.models import (
    SOURCE_SYSTEM,
    AcceptedInputs,
    DatasetRow,
    RowDisposition,
    duration_seconds,
    parse_utc,
)
from intelligence.datasets.selection import Selection


def select_identity(
    identities: tuple[IdentityRevision, ...], source_entity_id: str, cutoff: str
) -> tuple[IdentityRevision | None, str | None]:
    candidates = tuple(
        item
        for item in identities
        if item.source_entity_id == source_entity_id and _eligible_identity(item, cutoff)
    )
    if not candidates:
        return None, "identity_unavailable"
    order = max(_identity_order(item) for item in candidates)
    latest = tuple(item for item in candidates if _identity_order(item) == order)
    if len(latest) != 1:
        return None, "identity_ambiguous_latest_revision"
    newest = latest[0]
    if newest.link_status != "resolved" or not newest.person_reference_eligible:
        return None, f"identity_{newest.link_status}"
    if newest.hyperp_person_id is None:
        raise ValueError("eligible resolved identity lacks Person reference")
    return newest, None


def row(
    inputs: AcceptedInputs,
    source_entity_id: str,
    feature: Selection,
    horizon: Selection,
    identity: IdentityRevision | None,
    identity_reason: str | None,
    activities: tuple[ActivityEvidence, ...],
) -> DatasetRow:
    feature_record, horizon_record = feature.record, horizon.record
    count, calls, age, missingness = _activity_features(activities, inputs.request.feature_cutoff)
    label, label_reason = _label(horizon)
    disposition = _row_disposition(feature_record, identity, label)
    return DatasetRow(
        SOURCE_SYSTEM,
        inputs.deals.boundary.source_instance_id,
        source_entity_id,
        None if feature_record is None else feature_record.key.source_record_pk,
        None if horizon_record is None else horizon_record.key.source_record_pk,
        None if identity is None else identity.hyperp_person_id,
        None if feature_record is None else feature_record.category_id,
        None if feature_record is None else feature_record.stage_id,
        None if feature_record is None else feature_record.stage_semantic_id,
        _age(feature_record, "source_event_at", inputs.request.feature_cutoff),
        _age(feature_record, "source_effective_at", inputs.request.feature_cutoff),
        count,
        calls,
        age,
        "legacy_partial_snapshot",
        missingness,
        identity_reason,
        feature.reason,
        label,
        label_reason,
        disposition,
    )


def deal_version_dispositions(
    versions: tuple[DealReference, ...], feature: Selection, horizon: Selection
) -> list[dict[str, object]]:
    representatives = {
        item.key.source_record_pk for item in (feature.record, horizon.record) if item is not None
    }
    ambiguous = (
        feature.reason == "ambiguous_authoritative_version"
        or horizon.reason == "ambiguous_authoritative_version"
    )
    return [
        {
            "kind": "deal_version",
            "primary_disposition": "selected_representative"
            if item.key.source_record_pk in representatives
            else "ambiguous_version"
            if ambiguous
            else "superseded_or_excluded",
            "reason_code": _version_reason(feature, horizon) if ambiguous else None,
            "source_record_pk": item.key.source_record_pk,
        }
        for item in versions
    ]


def _eligible_identity(record: IdentityRevision, cutoff: str) -> bool:
    if record.availability != "known_by_cutoff":
        return False
    values = (record.effective_at, record.available_at, record.first_known_at)
    if any(value is None for value in values):
        return False
    try:
        limit = parse_utc(cutoff, "cutoff")
        return all(parse_utc(str(value), "identity temporal evidence") <= limit for value in values)
    except ValueError:
        return False


def _identity_order(record: IdentityRevision) -> tuple[object, ...]:
    if record.effective_at is None or record.available_at is None or record.first_known_at is None:
        raise ValueError("ineligible identity cannot be ordered")
    return (
        parse_utc(record.effective_at, "identity effective"),
        parse_utc(record.available_at, "identity available"),
        parse_utc(record.first_known_at, "identity known"),
        record.global_revision,
    )


def _row_disposition(
    feature: DealReference | None, identity: IdentityRevision | None, label: str | None
) -> RowDisposition:
    if feature is None:
        return "ineligible"
    if identity is None:
        return "unresolved"
    if label is None:
        return "censored"
    return "labeled"


def _age(record: DealReference | None, field: str, cutoff: str) -> int | None:
    if record is None:
        return None
    value = getattr(record, field)
    return None if value is None else duration_seconds(value, cutoff)


def _activity_features(
    values: tuple[ActivityEvidence, ...], cutoff: str
) -> tuple[int | None, int | None, int | None, str | None]:
    activities = tuple(item for item in values if item.record_type == "crm_history")
    calls = tuple(item for item in values if item.record_type == "call")
    if not activities:
        return None, None, None, "no_eligible_partial_archive_evidence"
    newest = max(item.event_at for item in activities)
    return len(activities), len(calls) if calls else None, duration_seconds(newest, cutoff), None


def _label(selection: Selection) -> tuple[str | None, str | None]:
    if selection.record is None:
        return None, selection.reason or "no_horizon_state"
    labels = {"P": "open", "S": "won", "F": "lost"}
    label = labels.get(selection.record.stage_semantic_id or "")
    return (label, None) if label is not None else (None, "unmapped_horizon_stage")


def _version_reason(feature: Selection, horizon: Selection) -> str:
    return f"feature={feature.reason or 'selected'};horizon={horizon.reason or 'selected'}"
