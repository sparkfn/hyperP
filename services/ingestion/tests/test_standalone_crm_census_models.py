"""Focused model and topology coverage for standalone CRM census control."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from src.bitrix_ingestion_models import BITRIX_STREAM_KEYS
from src.ingestion_config import BitrixOpenLinesConfig, standalone_crm_census_configuration_digest
from src.standalone_crm_census_models import (
    FrozenSourceWindow,
    StandaloneCrmBudgetSnapshot,
    StandaloneCrmChildEnvelope,
    StandaloneCrmTerminalAccounting,
    attempt_deadlines,
)
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncAuthoritySnapshot,
    SourceSyncCensusRequest,
    admitted_request_fingerprint,
)


def _budget() -> StandaloneCrmBudgetSnapshot:
    return StandaloneCrmBudgetSnapshot(10, 20, 30.0, 100, 200, 5, 300.0)


def _source() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        source_key="bitrix_chat",
        source_instance_id="portal-a",
        control_instance_id="portal-a",
        occurrence_key="occurrence-1",
        operator_id="operator",
        selected_kinds=("company", "contact"),
        policy_version="policy-v1",
        association_contract_version="crm-company-membership-snapshot-v1",
        configuration_digest="sha256:configuration",
        budget=_budget(),
    )


def test_exact_kinds_terminal_accounting_and_legacy_streams_remain_disjoint() -> None:
    assert _source().census_kind == "source_sync"
    assert (
        MappingPrepareCensusRequest(
            source_key="bitrix_chat",
            source_instance_id="portal-a",
            control_instance_id="portal-a",
            occurrence_key="prepare",
            operator_id="operator",
            policy_version="p",
            association_contract_version="a",
            configuration_digest="c",
            budget=_budget(),
            prepared_revision_id="revision-1",
            prepared_revision_digest="sha256:revision",
            expected_current_mapping_head_id=None,
        ).census_kind
        == "mapping_prepare"
    )
    assert (
        MappingRollbackCensusRequest(
            source_key="bitrix_chat",
            source_instance_id="portal-a",
            control_instance_id="portal-a",
            occurrence_key="rollback",
            operator_id="operator",
            policy_version="p",
            association_contract_version="a",
            configuration_digest="c",
            budget=_budget(),
            target_revision_id="revision-1",
            target_revision_digest="sha256:revision",
            expected_current_mapping_head_id="head-1",
            intended_prior_mapping_head_id=None,
        ).census_kind
        == "mapping_rollback"
    )
    assert BITRIX_STREAM_KEYS == {
        "crm_deals",
        "crm_activities",
        "openlines_conversations",
        "crm_stage_history",
    }
    assert StandaloneCrmTerminalAccounting(1, 0, 0, 0, 1).no_work_units == 1


def test_cross_kind_fingerprints_are_disjoint_and_selected_zero_is_not_unselected() -> None:
    source = _source()
    prepare = MappingPrepareCensusRequest(
        source_key="bitrix_chat",
        source_instance_id="portal-a",
        control_instance_id="portal-a",
        occurrence_key="occurrence-1",
        operator_id="operator",
        policy_version="policy-v1",
        association_contract_version="crm-company-membership-snapshot-v1",
        configuration_digest="sha256:configuration",
        budget=_budget(),
        prepared_revision_id="revision-1",
        prepared_revision_digest="sha256:revision",
        expected_current_mapping_head_id=None,
    )
    authority = SourceSyncAuthoritySnapshot("mapping-1", "sha256:mapping", None)
    assert admitted_request_fingerprint(source, authority) != admitted_request_fingerprint(
        prepare, None
    )
    window = FrozenSourceWindow(("company", "contact"), (("company", 7), ("contact", 0)))
    assert window.upper_id_for("contact") == 0
    with pytest.raises(ValueError, match="every selected"):
        FrozenSourceWindow(("company", "contact"), (("contact", 0),))


def test_immutable_envelope_and_budget_deadlines_reject_invalid_values() -> None:
    source = _source()
    envelope = StandaloneCrmChildEnvelope(
        census_id="census",
        generation=1,
        parent_fence_token=7,
        unit_kind="contact",
        upper_id=0,
        revision_id=None,
        publication_id="publication",
        task_id="task",
        payload_digest="sha256:payload",
        source_instance_id="portal-a",
        control_instance_id="portal-a",
    )
    assert envelope.upper_id == 0
    with pytest.raises(ValueError, match="frozen upper ID"):
        replace(envelope, revision_id="revision")
    with pytest.raises(ValueError, match="canonical sorted"):
        replace(source, selected_kinds=("contact", "company"))
    now = datetime(2026, 8, 27, tzinfo=UTC)
    attempt, occurrence = attempt_deadlines(now, _budget())
    assert attempt < occurrence


def test_dedicated_digest_does_not_change_legacy_digest_contract() -> None:
    base = BitrixOpenLinesConfig(source_instance_id="portal-a")
    changed = replace(base, standalone_crm_identity_max_calls_per_attempt=77)
    assert standalone_crm_census_configuration_digest(
        base
    ) != standalone_crm_census_configuration_digest(changed)


def test_source_authority_is_captured_at_admission_not_supplied_by_operator() -> None:
    source = _source()
    authority = SourceSyncAuthoritySnapshot("mapping-1", "sha256:mapping", "projection-1")
    first = admitted_request_fingerprint(source, authority)
    assert first == admitted_request_fingerprint(source, authority)
    with pytest.raises(ValueError, match="requires a captured authority"):
        admitted_request_fingerprint(source, None)


def test_mapping_census_fingerprint_rejects_source_authority() -> None:
    prepare = MappingPrepareCensusRequest(
        source_key="bitrix_chat",
        source_instance_id="portal-a",
        control_instance_id="portal-a",
        occurrence_key="prepare",
        operator_id="operator",
        policy_version="p",
        association_contract_version="a",
        configuration_digest="c",
        budget=_budget(),
        prepared_revision_id="revision-1",
        prepared_revision_digest="sha256:revision",
        expected_current_mapping_head_id=None,
    )
    with pytest.raises(ValueError, match="must not carry"):
        admitted_request_fingerprint(
            prepare, SourceSyncAuthoritySnapshot("mapping-1", "sha256:mapping", None)
        )


def test_parent_and_child_fence_tokens_are_distinct_checkpoint_authority() -> None:
    from src.standalone_crm_census_models import StandaloneCrmCheckpoint

    checkpoint = StandaloneCrmCheckpoint(
        census_id="census",
        unit_kind="contact",
        upper_id=5,
        last_committed_id=3,
        company_binding_after_contact_id=3,
        processed_count=1,
        skipped_count=0,
        failed_count=0,
        no_work_count=0,
        generation=2,
        parent_fence_token=11,
        child_fence_token=29,
        child_task_id="child-task",
    )
    assert checkpoint.parent_fence_token == 11
    assert checkpoint.child_fence_token == 29
    with pytest.raises(ValueError, match="child_fence_token"):
        replace(checkpoint, child_fence_token=0)
