"""Focused point-in-time and partial-history contracts for reviewed datasets."""

from __future__ import annotations

from dataclasses import replace

import pytest
from intelligence.cli import build_parser
from intelligence.crm.activities.models import ArchiveRecord, ParentReference
from intelligence.crm_deal_refs.models import Boundary, DealKey, DealReference, IdentityRevision
from intelligence.datasets.definition import compute, content_digest
from intelligence.datasets.models import AcceptedInputs, ActivityInput, DatasetRequest, DealInput


def _inputs(with_activity: bool) -> AcceptedInputs:
    boundary = Boundary(
        2,
        "crm-deal-refs-v2",
        "bitrix_chat",
        "bitrix-primary",
        "crm_deal_identity_v2",
        "2026-01-03T00:00:00Z",
        "2026-01-03T00:00:00Z",
        100,
        10,
        1,
        2,
        None,
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
    )
    feature = _deal(1, "P", "2026-01-01T00:00:00Z")
    horizon = _deal(2, "S", "2026-01-02T00:00:00Z")
    identity = IdentityRevision(
        "identity-a",
        1,
        "bitrix_chat",
        "bitrix-primary",
        "crm_deal_identity_v2",
        "42",
        "resolved",
        "00000000-0000-0000-0000-000000000001",
        "active",
        "2026-01-03T00:00:00Z",
        "reviewed_activation",
        1,
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:00:00Z",
        "known_by_cutoff",
        True,
    )
    record = _activity() if with_activity else None
    activities = ActivityInput(
        "checkpoint-a",
        "activity-run",
        "snapshot-a",
        "e" * 64,
        "f" * 64,
        "a" * 64,
        "b" * 64,
        "bitrix-primary",
        "bitrix_chat",
        () if record is None else (record,),
        frozenset() if record is None else frozenset({record.source_record_pk}),
        frozenset(),
        frozenset(),
    )
    return AcceptedInputs(
        DatasetRequest(
            "deal-run",
            "checkpoint-a",
            "activity-run",
            "crm-deal-state-v1",
            "2026-01-01T12:00:00Z",
            "2026-01-02T12:00:00Z",
            7,
        ),
        DealInput(
            "deal-run", boundary, "c" * 64, "d" * 64, "e" * 64, (feature, horizon), (identity,)
        ),
        activities,
    )


def _deal(version: int, semantic: str, instant: str) -> DealReference:
    return DealReference(
        "bitrix_chat",
        "bitrix-primary",
        "crm_deal_identity_v2",
        DealKey("bitrix-crm-deal-42", version, f"pk-{version}"),
        "sha256:" + "a" * 64,
        "deal",
        "42",
        None,
        "0",
        "stage-a",
        semantic,
        None,
        instant,
        instant,
        instant,
        instant,
        instant,
        None,
        "known_by_cutoff",
        True,
        None,
        None,
        "2026-01-03T00:00:00Z",
    )


def _activity() -> ArchiveRecord:
    return ArchiveRecord(
        "activity-a",
        "history-a",
        "1",
        "history-a-v1",
        "hash-a",
        "bitrix-primary",
        "bitrix_chat",
        "crm_history",
        "active",
        "activity",
        "call",
        "bitrix_crm_activity",
        "2",
        "bitrix_crm_activity_v2",
        "2026-01-01T06:00:00Z",
        "2026-01-01T06:00:00Z",
        "2026-01-01T06:00:00Z",
        ParentReference(
            None,
            "bitrix-primary",
            "bitrix-crm-deal-42",
            "crm_deal",
            "STORED_PARENT",
            "bitrix_chat",
        ),
        (),
        (),
        (),
        (),
        "2026-01-01T06:00:00Z",
        None,
    )


def test_independent_horizon_label_and_partial_activity_lower_bound() -> None:
    result = compute(_inputs(True))
    row = result.rows[0]
    assert row.label == "won"
    assert row.archived_activity_count_lower_bound == 1
    assert row.companion_call_count_lower_bound is None
    assert row.feature_source_record_pk == "pk-1"
    assert row.horizon_source_record_pk == "pk-2"
    assert row.selected_identity_global_revision == 1
    assert row.horizon_version_age_seconds == 43_200


def test_absent_partial_archive_evidence_is_null_not_zero_and_digest_is_stable() -> None:
    inputs = _inputs(False)
    first = compute(inputs)
    second = compute(inputs)
    assert first.rows[0].archived_activity_count_lower_bound is None
    assert first.rows[0].activity_missingness_reason == "no_eligible_partial_archive_evidence"
    assert content_digest(inputs, first) == content_digest(inputs, second)


def test_fixed_dataset_parser_rejects_arbitrary_commands() -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        (
            "dataset",
            "build",
            "--deal-refs-run-id",
            "deal-run",
            "--activities-checkpoint-id",
            "checkpoint-a",
            "--activities-accepted-run-id",
            "activity-run",
            "--definition",
            "crm-deal-state-v1",
            "--feature-cutoff",
            "2026-01-01T00:00:00Z",
            "--label-cutoff",
            "2026-01-02T00:00:00Z",
            "--seed",
            "1",
        )
    )
    assert arguments.dataset_command == "build"


def test_source_record_id_lineage_is_distinct_from_entity_id_and_graph_conflicts_exclude() -> None:
    inputs = _inputs(True)
    result = compute(inputs)
    assert result.rows[0].source_entity_id == "42"
    included = next(
        item for item in result.dispositions if item["source_record_pk"] == "activity-a"
    )
    assert included["primary_disposition"] == "feature_included"

    activity = inputs.activities.records[0]
    graph = ParentReference(
        None,
        "bitrix-primary",
        "other-deal",
        "crm_deal",
        "CHILD_OF",
        "bitrix_chat",
    )
    conflicted = replace(activity, child_parents=(graph,))
    altered = replace(
        inputs,
        activities=replace(
            inputs.activities,
            records=(conflicted,),
            accepted_ids=frozenset({conflicted.source_record_pk}),
        ),
    )
    disposition = compute(altered).dispositions[0]
    assert disposition["primary_disposition"] == "join_excluded"
    assert disposition["reason_code"] == "graph_deal_parent_source_record_id_conflict"

    optional_pk = replace(graph, source_record_id="bitrix-crm-deal-42", source_record_pk="graph-pk")
    accepted = replace(activity, child_parents=(optional_pk,))
    result = compute(replace(inputs, activities=replace(inputs.activities, records=(accepted,))))
    assert result.dispositions[0]["primary_disposition"] == "feature_included"


def test_join_corroboration_is_exact_and_calls_inherit_parent_state() -> None:
    inputs = _inputs(True)
    history = inputs.activities.records[0]
    assert compute(inputs).rows[0].included_activity_join_corroboration == "stored_parent_only"
    graph = ParentReference(
        None, "bitrix-primary", "bitrix-crm-deal-42", "crm_deal", "CHILD_OF", "bitrix_chat"
    )
    corroborated = replace(history, child_parents=(graph,))
    result = compute(
        replace(inputs, activities=replace(inputs.activities, records=(corroborated,)))
    )
    assert result.rows[0].included_activity_join_corroboration == "stored_parent_graph_corroborated"
    non_deal = replace(graph, record_type="other")
    result = compute(
        replace(
            inputs,
            activities=replace(
                inputs.activities, records=(replace(history, child_parents=(non_deal,)),)
            ),
        )
    )
    assert result.rows[0].included_activity_join_corroboration == "stored_parent_only"
    parent = ParentReference(
        history.source_record_pk,
        "bitrix-primary",
        history.source_record_id,
        "crm_history",
        "CHILD_OF",
        "bitrix_chat",
    )
    call = replace(
        history,
        source_record_pk="call-c",
        source_record_id="call-c",
        record_type="call",
        stored_parent=replace(parent, source_record_pk=None, relationship="STORED_PARENT"),
        child_parents=(parent,),
        details_parents=(replace(parent, relationship="DETAILS_HISTORY_ITEM"),),
    )
    result = compute(
        replace(
            inputs,
            activities=replace(
                inputs.activities,
                records=(corroborated, call),
                accepted_ids=frozenset({"activity-a", "call-c"}),
            ),
        )
    )
    assert result.rows[0].included_activity_join_corroboration == "stored_parent_graph_corroborated"
    second = replace(
        history,
        source_record_pk="activity-b",
        source_record_id="history-b",
        event_at="2026-01-01T05:00:00Z",
    )
    result = compute(
        replace(
            inputs,
            activities=replace(
                inputs.activities,
                records=(corroborated, second),
                accepted_ids=frozenset({"activity-a", "activity-b"}),
            ),
        )
    )
    assert result.rows[0].included_activity_join_corroboration == "mixed"


def test_offset_and_fractional_activity_ordering_uses_instants() -> None:
    inputs = _inputs(True)
    early = replace(
        inputs.activities.records[0], source_record_pk="early", event_at="2026-01-01T10:00:00+02:00"
    )
    late = replace(
        inputs.activities.records[0], source_record_pk="late", event_at="2026-01-01T08:30:00.123Z"
    )
    result = compute(
        replace(
            inputs,
            activities=replace(
                inputs.activities, records=(early, late), accepted_ids=frozenset({"early", "late"})
            ),
        )
    )
    assert result.rows[0].seconds_since_last_eligible_archived_activity == 12_599


def test_logical_duplicate_and_inconsistent_lineage_fail_closed() -> None:
    inputs = _inputs(False)
    duplicate = replace(inputs.deals.deals[0], key=DealKey("bitrix-crm-deal-42", 1, "other-pk"))
    with pytest.raises(ValueError, match="logical source version"):
        compute(
            replace(inputs, deals=replace(inputs.deals, deals=(inputs.deals.deals[0], duplicate)))
        )

    inconsistent = replace(inputs.deals.deals[1], source_entity_id="43")
    with pytest.raises(ValueError, match="inconsistent entity lineage"):
        compute(
            replace(
                inputs, deals=replace(inputs.deals, deals=(inputs.deals.deals[0], inconsistent))
            )
        )


def test_identity_supersession_and_future_evidence_never_fall_back() -> None:
    inputs = _inputs(False)
    prior = inputs.deals.identities[0]
    future = replace(
        prior,
        global_revision=2,
        link_status="unresolved",
        hyperp_person_id=None,
        effective_at="2026-01-01T06:00:00Z",
        available_at="2026-01-01T06:00:00Z",
        first_known_at="2026-01-01T06:00:00Z",
    )
    result = compute(replace(inputs, deals=replace(inputs.deals, identities=(prior, future))))
    assert result.rows[0].disposition == "unresolved"
    assert result.rows[0].identity_reason == "identity_unresolved"

    leaked = replace(future, effective_at="2026-01-03T00:00:00Z")
    result = compute(replace(inputs, deals=replace(inputs.deals, identities=(prior, leaked))))
    assert result.rows[0].person_id == prior.hyperp_person_id

    backdated = replace(
        future,
        effective_at="2025-12-01T00:00:00Z",
        available_at="2026-01-01T06:00:00Z",
        first_known_at="2026-01-01T06:00:00Z",
    )
    result = compute(replace(inputs, deals=replace(inputs.deals, identities=(prior, backdated))))
    assert result.rows[0].identity_reason == "identity_unresolved"


def test_graph_only_multiple_and_unresolved_parents_have_specific_exclusions() -> None:
    inputs = _inputs(True)
    activity = inputs.activities.records[0]
    graph = ParentReference(
        None, "bitrix-primary", "bitrix-crm-deal-42", "crm_deal", "CHILD_OF", "bitrix_chat"
    )
    graph_only = replace(
        activity,
        stored_parent=ParentReference(None, None, None, None, "STORED_PARENT", None),
        child_parents=(graph,),
    )
    multiple = replace(activity, child_parents=(graph, replace(graph, source_record_id="other")))
    unresolved = replace(
        activity,
        stored_parent=replace(activity.stored_parent, source_record_id="unknown-deal"),
    )
    for record, reason in (
        (graph_only, "graph_only_deal_parent"),
        (multiple, "graph_multiple_deal_candidates"),
        (unresolved, "stored_parent_deal_not_in_lineage"),
    ):
        result = compute(replace(inputs, activities=replace(inputs.activities, records=(record,))))
        assert result.dispositions[0]["reason_code"] == reason


def test_companion_call_and_post_cutoff_activity_do_not_leak_features() -> None:
    inputs = _inputs(True)
    history = inputs.activities.records[0]
    parent = ParentReference(
        history.source_record_pk,
        "bitrix-primary",
        history.source_record_id,
        "crm_history",
        "CHILD_OF",
        "bitrix_chat",
    )
    detail = replace(parent, relationship="DETAILS_HISTORY_ITEM")
    call = replace(
        history,
        source_record_pk="call-a",
        source_record_id="call-a",
        record_type="call",
        stored_parent=replace(parent, source_record_pk=None, relationship="STORED_PARENT"),
        child_parents=(parent,),
        details_parents=(detail,),
    )
    result = compute(
        replace(
            inputs,
            activities=replace(
                inputs.activities,
                records=(history, call),
                accepted_ids=frozenset({history.source_record_pk, call.source_record_pk}),
            ),
        )
    )
    assert result.rows[0].companion_call_count_lower_bound == 1

    future = replace(history, event_at="2026-01-01T18:00:00Z")
    result = compute(replace(inputs, activities=replace(inputs.activities, records=(future,))))
    assert result.rows[0].archived_activity_count_lower_bound is None
    assert result.dispositions[0]["primary_disposition"] == "temporally_excluded"

    eligible = history
    late_parent = replace(history, source_record_pk="late-history", event_at="2026-01-01T18:00:00Z")
    late_reference = replace(parent, source_record_pk="late-history", source_record_id="history-a")
    late_call = replace(
        call,
        source_record_pk="late-call",
        child_parents=(late_reference,),
        details_parents=(replace(late_reference, relationship="DETAILS_HISTORY_ITEM"),),
    )
    result = compute(
        replace(
            inputs,
            activities=replace(
                inputs.activities,
                records=(eligible, late_parent, late_call),
                accepted_ids=frozenset({"activity-a", "late-history", "late-call"}),
            ),
        )
    )
    assert result.rows[0].companion_call_count_lower_bound is None
    assert (
        next(item for item in result.dispositions if item["source_record_pk"] == "late-call")[
            "primary_disposition"
        ]
        == "join_excluded"
    )


def test_competing_source_lineages_for_one_entity_fail_closed() -> None:
    inputs = _inputs(False)
    first, second = inputs.deals.deals
    tied = replace(second, key=DealKey("bitrix-crm-deal-43", 1, "pk-3"))
    tied_second = replace(
        tied,
        source_effective_at=first.source_effective_at,
        observed_at=first.observed_at,
        available_at=first.available_at,
    )
    with pytest.raises(ValueError, match="competing source record lineages"):
        compute(replace(inputs, deals=replace(inputs.deals, deals=(first, tied_second))))
