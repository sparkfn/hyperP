"""Strict source-record lineage and activity join evidence for datasets."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from intelligence.crm.activities.models import ArchiveRecord, ParentReference
from intelligence.crm_deal_refs.models import DealReference
from intelligence.datasets.models import SOURCE_SYSTEM, AcceptedInputs, parse_instant


@dataclass(frozen=True)
class ActivityEvidence:
    event_at: str
    record_type: str
    source_record_pk: str
    join_corroboration: str


@dataclass(frozen=True)
class LineageTarget:
    source_entity_id: str
    source_system: str
    source_instance_id: str
    source_entity_type: str
    source_record_pks: frozenset[str]


@dataclass(frozen=True)
class Lineage:
    groups: dict[str, tuple[DealReference, ...]]
    by_source_record_id: dict[str, LineageTarget]


@dataclass(frozen=True)
class Join:
    target: str | None
    failure_reason: str | None
    corroboration: str | None


def deal_lineage(inputs: AcceptedInputs) -> Lineage:
    grouped: dict[str, list[DealReference]] = defaultdict(list)
    by_id: dict[str, LineageTarget] = {}
    entity_lineages: dict[str, str] = {}
    versions: set[tuple[str, int]] = set()
    pks: set[str] = set()
    for record in inputs.deals.deals:
        _validate_deal(record, inputs, versions, pks)
        prior_lineage = entity_lineages.get(record.source_entity_id)
        if prior_lineage is not None and prior_lineage != record.key.source_record_id:
            raise ValueError("source entity maps to competing source record lineages")
        entity_lineages[record.source_entity_id] = record.key.source_record_id
        candidate = LineageTarget(
            record.source_entity_id,
            record.source_system,
            record.source_instance_id,
            record.source_entity_type,
            frozenset({record.key.source_record_pk}),
        )
        _merge_lineage(by_id, record.key.source_record_id, candidate)
        grouped[record.source_entity_id].append(record)
    return Lineage({key: tuple(value) for key, value in grouped.items()}, by_id)


def activity_evidence(
    inputs: AcceptedInputs,
    lineage: Lineage,
) -> tuple[dict[str, tuple[ActivityEvidence, ...]], list[dict[str, object]]]:
    by_id = {record.source_record_pk: record for record in inputs.activities.records}
    if len(by_id) != len(inputs.activities.records):
        raise ValueError("activity inputs contain duplicate source record PKs")
    joined: dict[str, list[ActivityEvidence]] = defaultdict(list)
    targets: dict[str, tuple[str, str]] = {}
    dispositions: list[dict[str, object]] = []
    for record in sorted(
        inputs.activities.records,
        key=lambda item: (item.record_type == "call", item.source_record_pk),
    ):
        primary, target, reason, corroboration = _activity_primary(
            record, inputs, lineage, by_id, targets
        )
        dispositions.append(
            {
                "kind": "activity",
                "primary_disposition": primary,
                "reason_code": reason,
                "source_record_pk": record.source_record_pk,
            }
        )
        if primary == "feature_included" and target is not None:
            event = activity_event(record, inputs.request.feature_cutoff)
            if event is None:
                raise AssertionError("included activity lacks temporal evidence")
            joined[target].append(
                ActivityEvidence(
                    event,
                    record.record_type,
                    record.source_record_pk,
                    corroboration or "stored_parent_only",
                )
            )
    _assert_balance(inputs, dispositions)
    return {key: tuple(value) for key, value in joined.items()}, dispositions


def activity_event(record: ArchiveRecord, cutoff: str) -> str | None:
    evidence = (record.event_at, record.observed_at, record.ingested_at, record.available_at)
    if any(value is None for value in evidence):
        return None
    try:
        limit = parse_instant(cutoff, "cutoff")
        if not all(
            parse_instant(str(value), "activity temporal evidence") <= limit for value in evidence
        ):
            return None
    except ValueError:
        return None
    return record.event_at


def _validate_deal(
    record: DealReference,
    inputs: AcceptedInputs,
    versions: set[tuple[str, int]],
    pks: set[str],
) -> None:
    if (
        record.source_system != SOURCE_SYSTEM
        or record.source_instance_id != inputs.deals.boundary.source_instance_id
    ):
        raise ValueError("deal source lineage is incompatible with its boundary")
    version = (record.key.source_record_id, record.key.source_record_version)
    if version in versions:
        raise ValueError("duplicate deal logical source version")
    if record.key.source_record_pk in pks:
        raise ValueError("duplicate deal source record PK")
    versions.add(version)
    pks.add(record.key.source_record_pk)


def _merge_lineage(
    by_id: dict[str, LineageTarget], source_id: str, candidate: LineageTarget
) -> None:
    prior = by_id.get(source_id)
    if prior is None:
        by_id[source_id] = candidate
        return
    same = (
        prior.source_entity_id == candidate.source_entity_id
        and prior.source_system == candidate.source_system
        and prior.source_instance_id == candidate.source_instance_id
        and prior.source_entity_type == candidate.source_entity_type
    )
    if not same:
        raise ValueError("deal source record maps to inconsistent entity lineage")
    by_id[source_id] = LineageTarget(
        prior.source_entity_id,
        prior.source_system,
        prior.source_instance_id,
        prior.source_entity_type,
        prior.source_record_pks | candidate.source_record_pks,
    )


def _activity_primary(
    record: ArchiveRecord,
    inputs: AcceptedInputs,
    lineage: Lineage,
    by_id: dict[str, ArchiveRecord],
    targets: dict[str, tuple[str, str]],
) -> tuple[str, str | None, str | None, str | None]:
    if record.source_record_pk in inputs.activities.rejected_ids:
        return "source_rejected", None, "source_rejected", None
    if record.source_record_pk in inputs.activities.quarantined_ids:
        return "source_quarantined", None, "source_quarantined", None
    if record.source_record_pk not in inputs.activities.accepted_ids:
        raise ValueError("activity record lacks a source disposition")
    join = (
        stored_parent_target(record, inputs, lineage)
        if record.record_type == "crm_history"
        else call_parent_target(record, by_id, targets)
    )
    if join.target is None:
        return "join_excluded", None, join.failure_reason, None
    if activity_event(record, inputs.request.feature_cutoff) is None:
        return (
            "temporally_excluded",
            None,
            "activity_temporal_evidence_unavailable_or_after_cutoff",
            None,
        )
    if join.corroboration is None:
        raise AssertionError("successful join lacks corroboration")
    targets[record.source_record_pk] = (join.target, join.corroboration)
    return "feature_included", join.target, None, join.corroboration


def stored_parent_target(record: ArchiveRecord, inputs: AcceptedInputs, lineage: Lineage) -> Join:
    stored = record.stored_parent
    candidates = tuple(item for item in record.child_parents if item.record_type == "crm_deal")
    if (
        stored.source_system != SOURCE_SYSTEM
        or stored.source_instance_id != inputs.deals.boundary.source_instance_id
        or stored.record_type != "crm_deal"
        or stored.source_record_id is None
    ):
        return Join(
            None,
            "graph_only_deal_parent" if candidates else "missing_or_invalid_stored_parent",
            None,
        )
    target = lineage.by_source_record_id.get(stored.source_record_id)
    if target is None:
        return Join(None, "stored_parent_deal_not_in_lineage", None)
    if (
        stored.source_record_pk is not None
        and stored.source_record_pk not in target.source_record_pks
    ):
        return Join(None, "stored_parent_pk_not_in_lineage", None)
    if len(candidates) > 1:
        return Join(None, "graph_multiple_deal_candidates", None)
    if len(candidates) == 1:
        conflict = _graph_conflict(stored, candidates[0])
        if conflict is not None:
            return Join(None, conflict, None)
    return Join(
        target.source_entity_id,
        None,
        "stored_parent_graph_corroborated" if candidates else "stored_parent_only",
    )


def call_parent_target(
    record: ArchiveRecord, by_id: dict[str, ArchiveRecord], targets: dict[str, tuple[str, str]]
) -> Join:
    if len(record.child_parents) != 1 or len(record.details_parents) != 1:
        return Join(None, "call_parent_shape_invalid", None)
    child, detail = record.child_parents[0], record.details_parents[0]
    if (
        child.record_type != "crm_history"
        or detail.record_type != "crm_history"
        or child.source_record_pk is None
        or child.source_record_pk != detail.source_record_pk
        or child.source_record_pk not in by_id
        or by_id[child.source_record_pk].record_type != "crm_history"
    ):
        return Join(None, "call_parent_reference_invalid", None)
    parent = targets.get(child.source_record_pk)
    if parent is None:
        return Join(None, "call_parent_join_excluded", None)
    return Join(parent[0], None, parent[1])


def _graph_conflict(stored: ParentReference, graph: ParentReference) -> str | None:
    fields = (
        ("source_system", stored.source_system, graph.source_system),
        ("source_instance", stored.source_instance_id, graph.source_instance_id),
        ("source_record_id", stored.source_record_id, graph.source_record_id),
        ("record_type", stored.record_type, graph.record_type),
        ("source_record_pk", stored.source_record_pk, graph.source_record_pk),
    )
    for name, stored_value, graph_value in fields:
        if (
            (name != "source_record_pk" or stored_value is not None)
            and graph_value is not None
            and graph_value != stored_value
        ):
            return f"graph_deal_parent_{name}_conflict"
    return None


def _assert_balance(inputs: AcceptedInputs, dispositions: list[dict[str, object]]) -> None:
    selected = {item.source_record_pk for item in inputs.activities.records}
    source = (
        set(inputs.activities.accepted_ids)
        | set(inputs.activities.rejected_ids)
        | set(inputs.activities.quarantined_ids)
    )
    if selected != source or len(source) != len(inputs.activities.records):
        raise ValueError("activity selected/source disposition counts do not balance")
    actual = {str(item["source_record_pk"]) for item in dispositions}
    if actual != selected or len(actual) != len(dispositions):
        raise ValueError("activity dataset disposition identities do not balance")
    accepted = {
        str(item["source_record_pk"])
        for item in dispositions
        if item["primary_disposition"]
        in {"feature_included", "temporally_excluded", "join_excluded"}
    }
    if accepted != set(inputs.activities.accepted_ids):
        raise ValueError("accepted activity dataset dispositions do not balance")
