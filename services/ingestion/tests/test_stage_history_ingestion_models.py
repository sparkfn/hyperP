"""Invariant tests for typed CRM stage-history ingestion contracts."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_stage_history.models import StageHistoryItem
from src.stage_history_ingestion_models import (
    StageHistoryAccounting,
    StageHistoryMalformedObservation,
    StageHistoryOccurrence,
    StageHistoryReplaySourceWindow,
    StageHistoryReplayUnit,
    StageHistoryReviewCommand,
    StageHistoryValidObservation,
)

_DIGEST = "sha256:" + "a" * 64
_HMAC = "b" * 64
_NOW = datetime(2026, 8, 14, tzinfo=UTC)


def _item() -> StageHistoryItem:
    return StageHistoryItem(
        history_id="101",
        entity_type_id="2",
        owner_id="42",
        type_id="2",
        created_time=_NOW,
        created_time_source="CREATED_TIME",
        category_id="0",
        stage_semantic_id="P",
        stage_id="C0:NEW",
        raw_payload={"ID": "101", "OWNER_ID": "42"},
    )


def _valid_occurrence() -> StageHistoryOccurrence:
    observation = StageHistoryValidObservation(
        occurrence_id="occurrence-1",
        artifact_id="artifact-1",
        page_sequence=1,
        row_sequence=1,
        event_identity="bitrix-stage-history:2:101",
        canonical_hash=_DIGEST,
        item=_item(),
        logical_parent_source_system="bitrix_chat",
        logical_parent_source_record_id="bitrix-crm-deal-42",
        source_observed_at=_NOW,
    )
    return StageHistoryOccurrence(
        observation=observation,
        disposition="canonical_effective",
        parse_scope="in_scope",
        identity_hash_state="new_variant",
        association_state="selected_active",
        authority_state="effective",
    )


def test_replay_source_window_rejects_clear_or_invalid_contract_values() -> None:
    with pytest.raises(ValueError, match="artifact_manifest_hmac"):
        StageHistoryReplaySourceWindow(
            stage_ingestion_artifact_id="artifact-1",
            artifact_manifest_hmac="not-a-digest",
            source_contract_uuid="contract-1",
            entity_type_id="2",
            owner_artifact_id="owner-1",
            owner_manifest_digest=_HMAC,
            stage_artifact_id="stage-1",
            qualification_evidence_digest=_HMAC,
            canonical_hash_version="bitrix-stage-history-v1",
            traversal_contract="bounded_spool_reconcile",
            configuration_digest=_HMAC,
            limits_digest=_HMAC,
        )


def test_occurrence_rejects_terminal_and_dimension_disagreement() -> None:
    valid = _valid_occurrence().observation
    with pytest.raises(ValueError, match="identity/hash state disagree"):
        StageHistoryOccurrence(
            observation=valid,
            disposition="same_hash_replay",
            parse_scope="in_scope",
            identity_hash_state="new_variant",
            association_state="selected_active",
            authority_state="effective",
        )


def test_failed_capture_cannot_create_domain_dimensions() -> None:
    malformed = StageHistoryMalformedObservation(
        occurrence_id="malformed-1",
        artifact_id="failed-artifact-1",
        page_sequence=1,
        row_sequence=1,
        canonical_raw_row_digest=_DIGEST,
        safe_error_code="invalid_history_id",
        source_observed_at=_NOW,
    )
    with pytest.raises(ValueError, match="non-domain occurrences"):
        StageHistoryOccurrence(
            observation=malformed,
            disposition="malformed_excluded",
            parse_scope="malformed",
            identity_hash_state="new_variant",
        )


def test_replay_unit_derives_and_verifies_orthogonal_accounting() -> None:
    occurrence = _valid_occurrence()
    accounting = StageHistoryAccounting.from_occurrences((occurrence,))
    unit = StageHistoryReplayUnit(
        run_type="bounded_smoke_replay",
        unit_id="unit-1",
        artifact_id="artifact-1",
        page_sequence=1,
        page_digest=_DIGEST,
        occurrences=(occurrence,),
        accounting=accounting,
    )

    assert unit.accounting.terminal.fetched == 1
    assert unit.accounting.identity.new_variant == 1
    assert unit.accounting.association.selected_active == 1
    assert unit.accounting.authority.effective == 1
    assert unit.accounting.retry.none == 1


def test_failure_run_rejects_success_dispositions() -> None:
    occurrence = _valid_occurrence()
    with pytest.raises(ValueError, match="incompatible terminal disposition"):
        StageHistoryReplayUnit(
            run_type="capture_failure_accounting",
            unit_id="unit-1",
            artifact_id="artifact-1",
            page_sequence=1,
            page_digest=_DIGEST,
            occurrences=(occurrence,),
            accounting=StageHistoryAccounting.from_occurrences((occurrence,)),
        )


def test_valid_observation_requires_the_existing_crm_deal_source_identity() -> None:
    with pytest.raises(ValueError, match="CRM deal source identity"):
        StageHistoryValidObservation(
            occurrence_id="occurrence-1",
            artifact_id="artifact-1",
            page_sequence=1,
            row_sequence=1,
            event_identity="event-1",
            canonical_hash=_DIGEST,
            item=_item(),
            logical_parent_source_system="bitrix_chat",
            logical_parent_source_record_id="42",
            source_observed_at=_NOW,
        )


def test_parent_review_cannot_select_a_variant_or_wrong_authority_state() -> None:
    common = {
        "command_id": "command-1",
        "kind": "resolve_parent",
        "status": "pending",
        "event_identity": "event-1",
        "reviewer_id": "reviewer-1",
        "available_at": _NOW,
        "expected_head_version": 1,
        "expected_authority_token": 1,
        "expected_variant_set_digest": _DIGEST,
        "retry_sequence": 1,
    }
    with pytest.raises(ValueError, match="withheld-parent"):
        StageHistoryReviewCommand(
            **common,
            expected_authority_state="withheld_conflict",
        )
    with pytest.raises(ValueError, match="cannot select"):
        StageHistoryReviewCommand(
            **common,
            expected_authority_state="withheld_parent",
            selected_variant_hash=_DIGEST,
        )


def test_correction_requires_a_selected_association_before_persistence() -> None:
    with pytest.raises(ValueError, match="selected association"):
        StageHistoryReviewCommand(
            command_id="command-1",
            kind="apply_correction",
            status="pending",
            event_identity="event-1",
            reviewer_id="reviewer-1",
            available_at=_NOW,
            expected_head_version=1,
            expected_authority_token=1,
            expected_authority_state="withheld_conflict",
            expected_variant_set_digest=_DIGEST,
            selected_variant_hash=_DIGEST,
            correction_of_decision_id="authority-1",
        )
