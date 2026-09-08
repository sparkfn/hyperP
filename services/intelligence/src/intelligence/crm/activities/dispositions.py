"""Closed classification and exact partition checks for sealed records."""

from __future__ import annotations

from intelligence.crm.activities.models import ArchiveRecord, Disposition


def classify(records: tuple[ArchiveRecord, ...]) -> tuple[Disposition, ...]:
    """Classify records without retries, inferred Persons, or hidden remainder."""
    activities = {
        record.source_record_pk: record for record in records if record.record_type == "crm_history"
    }
    outcomes: list[Disposition] = []
    accepted_activities: set[str] = set()
    for record in records:
        if record.record_type != "crm_history":
            continue
        outcome = _base(record)
        outcomes.append(outcome)
        if outcome.disposition == "accepted":
            accepted_activities.add(record.source_record_pk)
    for record in records:
        if record.record_type != "call":
            continue
        outcome = _base(record)
        if outcome.disposition == "accepted":
            outcome = _call_outcome(record, activities, accepted_activities)
        outcomes.append(outcome)
    result = tuple(sorted(outcomes, key=lambda item: item.source_record_pk))
    assert_partition(records, result)
    return result


def _base(record: ArchiveRecord) -> Disposition:
    if not record.known_lifecycle:
        return Disposition(record.source_record_pk, "rejected", "unknown_lifecycle")
    return Disposition(record.source_record_pk, "accepted", None)


def _call_outcome(
    record: ArchiveRecord,
    activities: dict[str, ArchiveRecord],
    accepted: set[str],
) -> Disposition:
    child = tuple(item for item in record.child_parents if item.record_type == "crm_history")
    details = tuple(item for item in record.details_parents if item.record_type == "crm_history")
    if (
        len(record.child_parents) != 1
        or len(record.details_parents) != 1
        or len(child) != 1
        or len(details) != 1
    ):
        return Disposition(record.source_record_pk, "quarantined", "ambiguous_companion_parent")
    if child[0].source_record_pk is None or details[0].source_record_pk is None:
        return Disposition(record.source_record_pk, "quarantined", "unresolved_companion_parent")
    if child[0].source_record_pk != details[0].source_record_pk:
        return Disposition(record.source_record_pk, "quarantined", "conflicting_companion_parent")
    if (
        record.stored_parent.source_instance_id != child[0].source_instance_id
        or record.stored_parent.source_record_id != child[0].source_record_id
        or record.stored_parent.record_type != "crm_history"
    ):
        return Disposition(
            record.source_record_pk,
            "quarantined",
            "stored_companion_parent_conflict",
        )
    parent = activities.get(child[0].source_record_pk)
    if parent is None or child[0].source_instance_id != record.source_instance_id:
        return Disposition(
            record.source_record_pk,
            "quarantined",
            "cross_instance_companion_parent",
        )
    if parent.source_record_pk not in accepted:
        return Disposition(record.source_record_pk, "quarantined", "companion_parent_not_accepted")
    return Disposition(record.source_record_pk, "accepted", None)


def assert_partition(records: tuple[ArchiveRecord, ...], outcomes: tuple[Disposition, ...]) -> None:
    selected = {record.source_record_pk for record in records}
    outcome_ids = {outcome.source_record_pk for outcome in outcomes}
    if len(outcome_ids) != len(outcomes) or selected != outcome_ids:
        raise RuntimeError("selected identities do not have one exact disposition")
    buckets = {
        kind: {item.source_record_pk for item in outcomes if item.disposition == kind}
        for kind in ("accepted", "rejected", "quarantined")
    }
    if (
        buckets["accepted"] & buckets["rejected"]
        or buckets["accepted"] & buckets["quarantined"]
        or buckets["rejected"] & buckets["quarantined"]
    ):
        raise RuntimeError("disposition partition overlaps")
    if selected != buckets["accepted"] | buckets["rejected"] | buckets["quarantined"]:
        raise RuntimeError("disposition partition has unexplained remainder")
