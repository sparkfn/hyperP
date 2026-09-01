from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest
from src.graph.standalone_crm_census_records import terminal_window_expectations
from src.standalone_crm_census_models import (
    ContactSourceChildEnvelope,
    MappingPrepareAuthority,
    MappingPrepareCensusRequest,
    MappingRollbackAuthority,
    NoSourceWindow,
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    SourceWindow,
    StandaloneCrmAttempt,
    StandaloneCrmBudget,
    StandaloneCrmCallIntent,
    StandaloneCrmCallOutcome,
    StandaloneCrmCensus,
    StandaloneCrmCensusConflictError,
    StandaloneCrmCensusUnit,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
    StandaloneCrmContinuation,
    StandaloneCrmPublication,
    StandaloneCrmReason,
    StandaloneCrmReasonCode,
    StandaloneCrmStreamKind,
    StandaloneCrmTerminalAccounting,
    census_fingerprint,
    is_terminal_state,
    parse_census_request,
)
from src.standalone_crm_child_contracts import (
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildBudgetAuthorization,
    StandaloneCrmSourceChildScope,
    StandaloneCrmSourceChildUnitAuthority,
)


def _budget() -> StandaloneCrmBudget:
    return StandaloneCrmBudget(2, 3, 4, 5, 6, 7, "2026-08-29T00:00:00Z")


def _source_request(
    kinds: tuple[StandaloneCrmStreamKind, ...] = ("lead", "contact"),
) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        kinds,
        _budget(),
        "policy-v1",
        "association-v1",
        "sha256:" + "a" * 64,
        SourceSyncAuthority(
            "mapping-a",
            "sha256:" + "b" * 64,
            "projection-a",
            "sha256:" + "c" * 64,
        ),
    )


def _raw_source_request() -> dict[str, object]:
    return {
        "census_kind": "source_sync",
        "source_key": "bitrix_chat",
        "source_instance_id": "portal-a",
        "control_instance_id": "control-a",
        "occurrence_key": "occurrence-a",
        "selected_kinds": ["lead", "contact"],
        "budget": {
            "max_calls_per_attempt": 2,
            "max_rows_per_attempt": 3,
            "max_runtime_seconds_per_attempt": 4,
            "max_calls_per_occurrence": 5,
            "max_rows_per_occurrence": 6,
            "max_attempts_per_occurrence": 7,
            "occurrence_deadline": "2026-08-29T00:00:00+00:00",
        },
        "policy_version": "policy-v1",
        "association_contract_version": "association-v1",
        "configuration_digest": "sha256:" + "a" * 64,
        "authority": {
            "mapping_head_id": "mapping-a",
            "mapping_head_digest": "sha256:" + "b" * 64,
            "projection_head_id": "projection-a",
            "projection_head_digest": "sha256:" + "c" * 64,
        },
    }


def test_lifecycle_reason_codes_and_immutability() -> None:
    assert all(
        is_terminal_state(state)
        for state in ("completed", "failed", "cancelled_with_checkpoint", "freeze_failed")
    )
    assert not is_terminal_state("running")
    assert StandaloneCrmReason("authority_stale", "head changed").code == "authority_stale"
    reason_codes: tuple[StandaloneCrmReasonCode, ...] = (
        "authority_unavailable",
        "authority_stale",
        "budget_exhausted",
        "cancelled",
        "child_handler_unavailable",
        "freeze_incomplete",
        "publication_unsettled",
        "reservation_unknown",
        "stale_fence",
        "deadline_elapsed",
        "invalid_checkpoint",
        "source_disabled",
        "call_failed",
        "call_unknown",
        "handler_missing",
        "publication_failed",
        "recovery_required",
    )
    assert [StandaloneCrmReason(code, "detail").code for code in reason_codes] == list(reason_codes)
    assert (
        StandaloneCrmAttempt("census-a", 1, 1, "running", "2026-08-28T08:00:00Z", "task-a").state
        == "running"
    )
    window = SourceWindow((("contact", 0), ("lead", 9)))
    assert window.bound_for("contact") == 0
    assert window.bound_for("company") is None
    with pytest.raises(FrozenInstanceError):
        window.__setattr__("window_version", "changed")


def test_raw_parser_rejects_missing_extra_and_cross_kind_fields() -> None:
    parsed = parse_census_request(_raw_source_request())
    assert isinstance(parsed, SourceSyncCensusRequest)
    assert parsed.selected_kinds == ("contact", "lead")
    assert parsed.budget.occurrence_deadline == "2026-08-29T00:00:00Z"
    missing = _raw_source_request()
    del missing["policy_version"]
    with pytest.raises(ValueError, match="missing=.*policy_version"):
        parse_census_request(missing)
    extra = _raw_source_request()
    extra["unexpected"] = "nope"
    with pytest.raises(ValueError, match="extra=.*unexpected"):
        parse_census_request(extra)
    cross_kind = _raw_source_request()
    authority = cross_kind["authority"]
    assert isinstance(authority, dict)
    authority["prepared_revision_id"] = "wrong-kind"
    with pytest.raises(ValueError, match="extra=.*prepared_revision_id"):
        parse_census_request(cross_kind)
    missing_authority = _raw_source_request()
    authority = missing_authority["authority"]
    assert isinstance(authority, dict)
    del authority["projection_head_id"]
    with pytest.raises(ValueError, match="missing=.*projection_head_id"):
        parse_census_request(missing_authority)


def test_raw_parser_builds_each_discriminated_request_kind() -> None:
    prepare = _raw_source_request()
    prepare["census_kind"] = "mapping_prepare"
    prepare["selected_kinds"] = ["contact"]
    prepare["authority"] = {
        "prepared_revision_id": "prepared-a",
        "prepared_revision_digest": "digest-a",
        "expected_current_head_id": "head-a",
    }
    parsed_prepare = parse_census_request(prepare)
    assert isinstance(parsed_prepare, MappingPrepareCensusRequest)
    rollback = _raw_source_request()
    rollback["census_kind"] = "mapping_rollback"
    rollback["selected_kinds"] = ["lead"]
    rollback["authority"] = {
        "target_revision_id": "target-a",
        "target_revision_digest": "digest-a",
        "expected_current_head_id": "head-a",
        "rollback_head_id": "rollback-a",
    }
    assert parse_census_request(rollback).census_kind == "mapping_rollback"


def test_fixed_fingerprint_vector_and_domain_separation() -> None:
    source = _source_request()
    assert census_fingerprint(source) == (
        "sha256:3c587b318d457c112c1c705717ddc54fd041dfe863b717e350c68950dce338f8"
    )
    assert census_fingerprint(source) == census_fingerprint(_source_request(("contact", "lead")))
    prepare = MappingPrepareCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("contact",),
        _budget(),
        "policy-v1",
        "association-v1",
        "sha256:" + "a" * 64,
        MappingPrepareAuthority("revision-a", "sha256:" + "d" * 64, "mapping-a"),
    )
    assert census_fingerprint(source) != census_fingerprint(prepare)
    assert (
        MappingRollbackAuthority("target", "digest", "head", "rollback").rollback_head_id
        == "rollback"
    )


def test_budget_window_checkpoint_and_continuation_constraints() -> None:
    with pytest.raises(ValueError, match="integer"):
        StandaloneCrmBudget(True, 3, 4, 5, 6, 7, "2026-08-29T00:00:00Z")
    with pytest.raises(ValueError, match="timezone-aware"):
        StandaloneCrmBudget(2, 3, 4, 5, 6, 7, "2026-08-29T00:00:00")
    with pytest.raises(ValueError, match="attempt calls"):
        StandaloneCrmBudget(6, 3, 4, 5, 6, 7, "2026-08-29T00:00:00Z")
    with pytest.raises(ValueError, match="source upper bound"):
        SourceWindow((("contact", True),))
    assert NoSourceWindow("revision-a", "digest-a").revision_id == "revision-a"
    checkpoint = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 5, 0, 4, 1, 1, 2)
    advanced = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 6, 5, 1, 5, 1, 2, 3)
    assert checkpoint.can_advance_to(advanced)
    with pytest.raises(ValueError, match="exceed frozen"):
        StandaloneCrmCheckpoint("census-a", "contact", 10, None, 11, None, None, 4, 1, 1, 2)
    with pytest.raises(ValueError, match="skipped_rows"):
        StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, None, None, 1, 2, 1, 1)
    assert StandaloneCrmContinuation("census-a", 1, 2, "2026-08-28T00:00:00Z").next_generation == 2
    envelope = StandaloneCrmChildEnvelope(
        "census-a", 1, "contact", 0, None, "task", "id", "ingestion"
    )
    assert envelope.payload_digest() == (
        "sha256:750b84d0cabcb1139eb412691251e50cda93e97474ac76a2a313276fc6b1e470"
    )


def test_v1_child_envelope_remains_separate_from_source_child_execution_authority() -> None:
    v1 = StandaloneCrmChildEnvelope("census-a", 1, "contact", 0, None, "task", "id", "ingestion")
    assert v1.payload_version == "standalone-crm-child-v1"
    assert v1.payload_digest() == (
        "sha256:750b84d0cabcb1139eb412691251e50cda93e97474ac76a2a313276fc6b1e470"
    )
    scope = StandaloneCrmSourceChildScope("bitrix_chat", "portal-a", "control-a")
    unit = StandaloneCrmSourceChildUnitAuthority(
        "census-a", "contact", 1, 2, "owner-a", "task", "id", "sha256:" + "a" * 64
    )
    authorization = StandaloneCrmSourceChildBudgetAuthorization(
        "authorization-a",
        "sha256:" + "b" * 64,
        "census-a",
        "contact",
        1,
        2,
        "owner-a",
        "task",
        "id",
        "sha256:" + "a" * 64,
        2,
        3,
        4,
        5,
        "2026-08-28T01:00:00Z",
        "2026-08-29T00:00:00Z",
    )
    source = ContactSourceChildEnvelope(
        scope,
        unit,
        0,
        0,
        StandaloneCrmSourceAvailability("2026-08-28T00:00:00Z"),
        authorization,
    )
    assert source.unit.task_id == v1.task_id
    assert not isinstance(v1, ContactSourceChildEnvelope)


def test_call_intent_and_outcome_cross_field_rules() -> None:
    probe = StandaloneCrmCallIntent(
        "census-a", 1, "probe", 1, "probe", "lead", 0, "2026-08-28T00:00:00Z"
    )
    assert probe.stream_kind == "lead"
    page = StandaloneCrmCallIntent(
        "census-a",
        1,
        "page",
        2,
        "page",
        "company",
        0,
        "2026-08-28T00:00:00Z",
        0,
        None,
        "child-task-a",
    )
    assert page.cursor == 0
    with pytest.raises(ValueError, match="child task_id"):
        StandaloneCrmCallIntent(
            "census-a", 1, "page-no-task", 2, "page", "company", 0, "2026-08-28T00:00:00Z", 0
        )
    with pytest.raises(ValueError, match="company_binding"):
        StandaloneCrmCallIntent(
            "census-a",
            1,
            "bad",
            3,
            "company_binding",
            "lead",
            0,
            "2026-08-28T00:00:00Z",
            0,
            42,
        )
    assert (
        StandaloneCrmCallOutcome("probe", "probe", "succeeded", "2026-08-28T00:00:00Z", 0).upper_id
        == 0
    )
    with pytest.raises(ValueError, match="successful probe"):
        StandaloneCrmCallOutcome("probe", "probe", "succeeded", "2026-08-28T00:00:00Z")
    with pytest.raises(ValueError, match="failed or unknown"):
        StandaloneCrmCallOutcome("page", "page", "failed", "2026-08-28T00:00:00Z")


def test_full_stream_selection_uses_canonical_probe_order_and_fingerprint() -> None:
    first = _source_request(("company", "lead", "contact"))
    second = _source_request(("contact", "company", "lead"))

    assert first.selected_kinds == ("contact", "lead", "company")
    assert second.selected_kinds == ("contact", "lead", "company")
    assert census_fingerprint(first) == census_fingerprint(second)


def test_terminal_expectations_require_the_complete_canonical_source_selection() -> None:
    request = _source_request(("company", "lead", "contact"))
    window_json = json.dumps(
        {
            "selected_bounds": [["contact", 3], ["lead", 8], ["company", 11]],
            "window_version": "standalone-crm-source-window-v1",
        }
    )

    assert terminal_window_expectations(request, window_json) == [
        {"stream_kind": "contact", "frozen_upper_id": 3, "revision_id": None},
        {"stream_kind": "lead", "frozen_upper_id": 8, "revision_id": None},
        {"stream_kind": "company", "frozen_upper_id": 11, "revision_id": None},
    ]

    missing_selected_stream = json.dumps({"selected_bounds": [["contact", 3], ["lead", 8]]})
    out_of_order_selection = json.dumps(
        {"selected_bounds": [["company", 11], ["contact", 3], ["lead", 8]]}
    )
    for malformed_window in (missing_selected_stream, out_of_order_selection):
        with pytest.raises(
            StandaloneCrmCensusConflictError, match="stored source window selection conflicts"
        ):
            terminal_window_expectations(request, malformed_window)


def test_checkpoint_binding_cannot_be_cleared_or_regressed_after_progress() -> None:
    checkpoint = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 42, 3, 5, 1, 1, 7)
    cleared = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 6, None, None, 6, 1, 1, 7)
    regressed_subject = StandaloneCrmCheckpoint(
        "census-a", "contact", 10, None, 6, 41, 9, 6, 1, 1, 7
    )
    regressed_offset = StandaloneCrmCheckpoint(
        "census-a", "contact", 10, None, 6, 42, 2, 6, 1, 1, 7
    )
    advanced = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 6, 42, 4, 6, 1, 1, 7)

    assert not checkpoint.can_advance_to(cleared)
    assert not checkpoint.can_advance_to(regressed_subject)
    assert not checkpoint.can_advance_to(regressed_offset)
    assert checkpoint.can_advance_to(advanced)


def test_unit_publication_and_terminal_accounting_constraints() -> None:
    unit = StandaloneCrmCensusUnit("census-a", 1, "contact", "running", 0, None)
    assert unit.frozen_upper_id == 0
    publication = StandaloneCrmPublication(
        "census-a", 1, "contact", "task-a", "sha256:" + "f" * 64, "published"
    )
    assert publication.state == "published"
    accounting = StandaloneCrmTerminalAccounting(
        expected_units=3,
        processed_rows=8,
        skipped_rows=2,
        failed_rows=1,
        no_work_units=1,
        completed_units=1,
        failed_units=1,
    )
    assert accounting.settled_units == 3
    assert accounting.can_terminalize("failed")
    census = StandaloneCrmCensus(
        "census-a",
        _source_request(),
        "failed",
        "2026-08-28T00:00:00Z",
        StandaloneCrmReason("call_failed", "remote response failed"),
        accounting,
    )
    assert census.state == "failed"
    blocked = StandaloneCrmTerminalAccounting(1, 0, 0, 0, 1, unresolved_publications=1)
    assert not blocked.can_terminalize("completed")
    with pytest.raises(ValueError, match="settled units"):
        StandaloneCrmTerminalAccounting(1, 0, 0, 0, 2)
    with pytest.raises(ValueError, match="exactly one"):
        StandaloneCrmCensusUnit("census-a", 1, "contact", "queued", None, None)
    with pytest.raises(ValueError, match="terminal census"):
        StandaloneCrmCensus("census-a", _source_request(), "completed", "2026-08-28T00:00:00Z")


def test_legacy_and_v2_authority_contexts_preserve_contract_identity() -> None:
    from src.graph.standalone_crm_census_records import authority_context, authority_revision
    from src.standalone_crm_census_requests import (
        MappingPrepareAuthority,
        MappingPrepareCensusRequest,
        MappingRollbackAuthority,
        MappingRollbackCensusRequest,
        SourceSyncAuthority,
        SourceSyncCensusRequest,
        StandaloneCrmBudget,
    )

    budget = StandaloneCrmBudget(1, 1, 1, 1, 1, 1, "2026-09-02T00:00:00Z")
    source = SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "default",
        "legacy-source",
        ("contact",),
        budget,
        "p",
        "a",
        "c",
        SourceSyncAuthority("mapping", "sha256:" + "a" * 64, "projection", "sha256:" + "b" * 64),
    )
    assert (
        authority_context(source)
        == '{"mapping_head_digest":"sha256:'
        + "a" * 64
        + '","mapping_head_id":"mapping","projection_head_digest":"sha256:'
        + "b" * 64
        + '","projection_head_id":"projection"}'
    )
    legacy_rollback = MappingRollbackCensusRequest(
        "bitrix_chat",
        "portal-a",
        "default",
        "legacy-rollback",
        ("lead",),
        budget,
        "p",
        "a",
        "c",
        MappingRollbackAuthority("target", "sha256:" + "c" * 64, "head", "rollback"),
    )
    assert authority_revision(legacy_rollback) == "sha256:" + "c" * 64
    v2_prepare = MappingPrepareCensusRequest(
        "bitrix_chat",
        "portal-a",
        "default",
        "v2",
        ("lead",),
        budget,
        "p",
        "a",
        "c",
        MappingPrepareAuthority(
            "prepared",
            "sha256:" + "d" * 64,
            "head",
            "release",
            "sha256:" + "e" * 64,
            None,
            None,
            None,
            "projection-head",
            None,
            None,
            None,
        ),
    )
    assert '"completed_release_id":"release"' in authority_context(v2_prepare)
    assert '"expected_mapping_active_revision_id":null' in authority_context(v2_prepare)
