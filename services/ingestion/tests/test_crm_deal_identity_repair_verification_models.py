"""Pure contracts for #311 verification accounting and deterministic identities."""

from __future__ import annotations

from typing import cast

import pytest
from neo4j import ManagedTransaction, Record
from src.crm_deal_identity_repair.digests import inventory_digest
from src.crm_deal_identity_repair.execution_records import RepairFence, RepairUnit
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.crm_deal_identity_repair.mutation_models import build_inventory_binding_digest
from src.crm_deal_identity_repair.verification_models import (
    RepairRunEquationCommand,
    RepairRunEquationResult,
    RepairUnitEquation,
    RepairVerificationCommand,
)
from src.graph.crm_deal_identity_repair_ledger_records import canonical_json_text
from src.graph.crm_deal_identity_repair_verification_run import canonical_source_record_pks_json
from src.graph.crm_deal_identity_repair_verification_support import primary_matches

_DIGEST = "sha256:" + "1" * 64


def _item(partition: str = "ownership_repair") -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-7",
        source_record_pk="source-7",
        deal_id="7",
        partition=cast(RepairPartition, partition),
        graph_fingerprint=_DIGEST,
        stored_payload_fingerprint=_DIGEST,
        payload={"descendants": []},
    )


def _command() -> RepairVerificationCommand:
    item = _item()
    unit = RepairUnit(
        "run",
        "unit",
        1,
        0,
        1,
        _DIGEST,
        _DIGEST,
        "allocated",
        item.inventory_key,
        item.source_record_pk,
        item.graph_fingerprint,
        item.stored_payload_fingerprint,
        build_inventory_binding_digest(item),
    )
    fence = RepairFence(
        "run", "unit", "fence", 1, 0, 1, "owner", "token", _DIGEST, _DIGEST, "claimed"
    )
    return RepairVerificationCommand(unit, fence, item, "source", "control", "owner", "claim")


def test_command_child_ids_and_request_are_deterministic() -> None:
    first = _command()
    second = _command()
    assert first.request_digest == second.request_digest
    assert first.verification_id == second.verification_id
    assert first.outbox_event_id == second.outbox_event_id


def test_command_rejects_negative_control() -> None:
    item = _item("negative_control")
    unit = RepairUnit("run", "unit", 1, 0, 1, _DIGEST, _DIGEST, "allocated")
    fence = RepairFence(
        "run", "unit", "fence", 1, 0, 1, "owner", "token", _DIGEST, _DIGEST, "claimed"
    )
    with pytest.raises(ValueError, match="negative-control"):
        RepairVerificationCommand(unit, fence, item, "source", "control", "owner", "claim")


def test_unit_equation_requires_zero_unexplained_remainder() -> None:
    balanced = RepairUnitEquation(1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0, 0)
    unbalanced = RepairUnitEquation(1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 1)
    assert balanced.balanced
    assert not unbalanced.balanced


def test_run_equation_balances_only_exact_negative_controls() -> None:
    result = RepairRunEquationResult(
        qualified_inventory_rows=2,
        executable_inventory_rows=1,
        negative_control_rows=1,
        applied_units=1,
        review_required_units=0,
        incomplete_units=0,
        verified_units=1,
        drifted_units=0,
        failed_units=0,
        committed_attempts=1,
        replay_no_op_attempts=0,
        active_links=1,
        unsupported_multi_links=0,
        active_deal_origin_phone_projections=0,
        active_deal_origin_email_projections=0,
        active_deal_origin_g_us_projections=0,
        reconciled_secondaries=0,
        review_required_secondaries=0,
        failed_secondaries=0,
        pending_secondaries=0,
        expected_secondary_count=0,
        observed_secondary_count=0,
        unexplained_secondary_remainder=0,
        unchanged_negative_controls=1,
        drifted_negative_controls=0,
        missing_negative_controls=0,
        stamped_negative_controls=0,
        evidence_digest=_DIGEST,
    )
    assert result.balanced
    drifted = RepairRunEquationResult(
        **{**result.__dict__, "unchanged_negative_controls": 0, "drifted_negative_controls": 1}
    )
    assert not drifted.balanced


def test_run_boundary_parameter_uses_the_exact_300_canonical_object_shape() -> None:
    item = _item()
    assert canonical_source_record_pks_json((item,)) == canonical_json_text(
        {"source_record_pks": [item.source_record_pk]},
        "test source record identities",
    )


def test_run_command_uses_inventory_key_order_but_sorts_pk_boundary_separately() -> None:
    first = RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-1",
        source_record_pk="z-pk",
        deal_id="1",
        partition="negative_control",
        graph_fingerprint=_DIGEST,
        stored_payload_fingerprint=_DIGEST,
        payload={"descendants": []},
    )
    second = RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-2",
        source_record_pk="a-pk",
        deal_id="2",
        partition="negative_control",
        graph_fingerprint=_DIGEST,
        stored_payload_fingerprint=_DIGEST,
        payload={"descendants": []},
    )
    command = RepairRunEquationCommand(
        "repair",
        "run",
        _DIGEST,
        (first, second),
        inventory_digest((first, second)),
        "source",
        "control",
    )
    assert command.inventory == (first, second)
    assert canonical_source_record_pks_json(command.inventory) == canonical_json_text(
        {"source_record_pks": ["a-pk", "z-pk"]}, "test sorted source record identities"
    )


def test_primary_review_required_rejects_extra_inactive_person_links() -> None:
    base: dict[str, object] = {
        "link_status": "pending_review",
        "active_links": 0,
        "active_any_links": 0,
        "provisional_links": 1,
        "all_links": 1,
        "active_new_evidence": 0,
        "repair_review_count": 1,
        "repair_decision_count": 1,
        "retirement_stamp_failure_count": 0,
        "forbidden_projection_count": 0,
    }
    assert primary_matches(cast(Record, base), "review_required")
    for all_links in (0, 2):
        values = {**base, "all_links": all_links}
        assert not primary_matches(cast(Record, values), "review_required")


def test_primary_applied_allows_active_replacement_outside_retirement_domain() -> None:
    row: dict[str, object] = {
        "link_status": "linked",
        "active_links": 1,
        "active_any_links": 1,
        "provisional_links": 0,
        "all_links": 1,
        "active_new_evidence": 0,
        "repair_review_count": 0,
        "repair_decision_count": 0,
        "retirement_stamp_failure_count": 0,
        "forbidden_projection_count": 0,
    }
    assert primary_matches(cast(Record, row), "applied")
    assert not primary_matches(cast(Record, {**row, "link_status": "applied"}), "applied")


@pytest.mark.parametrize(
    "field,value",
    (
        ("replay_no_op_count", 1),
        ("drift_count", 1),
        ("expected_active_replacement_links", 0),
        ("active_provisional_links", 1),
    ),
)
def test_unit_equation_rejects_attempt_link_and_provisional_contradictions(
    field: str, value: int
) -> None:
    baseline = RepairUnitEquation(1, 1, 0, 0, 1, 0, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0, 0)
    values = dict(baseline.__dict__)
    values[field] = value
    assert not RepairUnitEquation(**values).balanced


@pytest.mark.parametrize(
    "field,value",
    (
        ("verified_units", 0),
        ("active_links", 0),
        ("committed_attempts", 0),
        ("expected_secondary_count", 1),
        ("observed_secondary_count", 1),
    ),
)
def test_run_equation_rejects_core_accounting_contradictions(field: str, value: int) -> None:
    baseline = RepairRunEquationResult(
        qualified_inventory_rows=2,
        executable_inventory_rows=1,
        negative_control_rows=1,
        applied_units=1,
        review_required_units=0,
        incomplete_units=0,
        verified_units=1,
        drifted_units=0,
        failed_units=0,
        committed_attempts=1,
        replay_no_op_attempts=0,
        active_links=1,
        unsupported_multi_links=0,
        active_deal_origin_phone_projections=0,
        active_deal_origin_email_projections=0,
        active_deal_origin_g_us_projections=0,
        reconciled_secondaries=0,
        review_required_secondaries=0,
        failed_secondaries=0,
        pending_secondaries=0,
        expected_secondary_count=0,
        observed_secondary_count=0,
        unexplained_secondary_remainder=0,
        unchanged_negative_controls=1,
        drifted_negative_controls=0,
        missing_negative_controls=0,
        stamped_negative_controls=0,
        evidence_digest=_DIGEST,
    )
    assert not RepairRunEquationResult(**{**baseline.__dict__, field: value}).balanced


def test_run_equation_allows_authenticated_current_replay_attempt() -> None:
    baseline = RepairRunEquationResult(
        qualified_inventory_rows=1,
        executable_inventory_rows=1,
        negative_control_rows=0,
        applied_units=1,
        review_required_units=0,
        incomplete_units=0,
        verified_units=1,
        drifted_units=0,
        failed_units=0,
        committed_attempts=1,
        replay_no_op_attempts=1,
        active_links=1,
        unsupported_multi_links=0,
        active_deal_origin_phone_projections=0,
        active_deal_origin_email_projections=0,
        active_deal_origin_g_us_projections=0,
        reconciled_secondaries=0,
        review_required_secondaries=0,
        failed_secondaries=0,
        pending_secondaries=0,
        expected_secondary_count=0,
        observed_secondary_count=0,
        unexplained_secondary_remainder=0,
        unchanged_negative_controls=0,
        drifted_negative_controls=0,
        missing_negative_controls=0,
        stamped_negative_controls=0,
        evidence_digest=_DIGEST,
    )
    assert baseline.balanced


def test_public_execution_contract_exports_resolve_after_model_split() -> None:
    from src.crm_deal_identity_repair.execution_models import (
        RepairAtomicVerificationResult,
        RepairRunEquationCommand,
        RepairRunEquationResult,
        RepairSecondarySubject,
        RepairUnitEquation,
        RepairVerificationCommand,
    )
    from src.crm_deal_identity_repair.execution_protocols import RepairVerificationRepository

    assert RepairVerificationRepository.__name__ == "RepairVerificationRepository"
    assert RepairVerificationCommand.__name__ == "RepairVerificationCommand"
    assert RepairAtomicVerificationResult.__name__ == "RepairAtomicVerificationResult"
    assert RepairSecondarySubject.__name__ == "RepairSecondarySubject"
    assert RepairUnitEquation.__name__ == "RepairUnitEquation"
    assert RepairRunEquationCommand.__name__ == "RepairRunEquationCommand"
    assert RepairRunEquationResult.__name__ == "RepairRunEquationResult"


def test_derived_state_digest_binds_all_rebuilt_golden_profile_fields() -> None:
    from src.graph.crm_deal_identity_repair_verification_derived import (
        PersonDerivedState,
        derive_state_digest,
    )

    primary = cast(
        Record,
        {
            "active_links": 1,
            "active_any_links": 1,
            "provisional_links": 0,
            "all_links": 1,
            "active_new_evidence": 0,
            "repair_review_count": 0,
            "repair_decision_count": 0,
            "retired_relationship_count": 0,
            "retirement_stamp_failure_count": 0,
            "forbidden_projection_count": 0,
        },
    )
    profile: dict[str, object] = {
        "preferred_full_name": "Name",
        "preferred_dob": None,
        "preferred_phone": None,
        "preferred_email": None,
        "preferred_address_id": None,
        "preferred_nric": "nric-a",
        "preferred_race_ethnicity": "race-a",
        "profile_completeness_score": 0.7,
        "golden_profile_version": "v0.1.0",
    }
    baseline = PersonDerivedState("person-a", 1, 1, None, profile)
    expected = derive_state_digest(primary, (), (baseline,), ())
    for field, value in (
        ("preferred_nric", "nric-b"),
        ("preferred_race_ethnicity", "race-b"),
        ("profile_completeness_score", 0.8),
    ):
        changed = PersonDerivedState("person-a", 1, 1, None, {**profile, field: value})
        assert derive_state_digest(primary, (), (changed,), ()) != expected


def test_replay_rejects_changed_override_conflict_with_same_displayed_profile() -> None:
    from src.graph.crm_deal_identity_repair_verification_derived import (
        PersonDerivedState,
        build_person_details,
    )
    from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
    from src.graph.crm_deal_identity_repair_verification_replay import (
        _validate_replayed_person_dispositions,
    )

    command = _command()
    state = PersonDerivedState(
        "person-a",
        1,
        4,
        '{"preferred_full_name":{"source_record_pk":"active-source"}}',
        {
            "preferred_full_name": "Fallback",
            "preferred_dob": None,
            "preferred_phone": None,
            "preferred_email": None,
            "preferred_address_id": None,
            "preferred_nric": None,
            "preferred_race_ethnicity": None,
            "profile_completeness_score": 0.2,
            "golden_profile_version": "v0.1.0",
        },
    )
    details = build_person_details(command, (state,), {"person-a": ()})
    persisted = [
        {
            "subject_kind": detail.subject.kind,
            "subject_stable_id": detail.subject.stable_id,
            "subject_fingerprint": detail.record(command).subject_fingerprint,
            "evidence_digest": detail.record(command).evidence_digest,
            "payload_digest": detail.record(command).payload_digest,
            "action": detail.action,
            "outcome": detail.outcome,
        }
        for detail in details
    ]
    _validate_replayed_person_dispositions(command, (state,), {"person-a": ()}, persisted)
    with pytest.raises(RepairVerificationDriftError, match="person disposition differs"):
        _validate_replayed_person_dispositions(
            command,
            (state,),
            {"person-a": ("preferred_full_name",)},
            persisted,
        )


def test_replayed_atomic_result_requires_balanced_current_operation_equation() -> None:
    from src.crm_deal_identity_repair.verification_models import RepairAtomicVerificationResult

    equation = RepairUnitEquation(1, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0, 0)
    with pytest.raises(ValueError, match="incoherent"):
        RepairAtomicVerificationResult("replayed", None, (), None, equation, _DIGEST)


def test_retirement_snapshot_preserves_inactive_prior_stamp_but_requires_active_stamp() -> None:
    from src.graph.crm_deal_identity_repair_verification_support import retirement_snapshot_matches

    base = {
        "relationship_type": "LINKED_TO",
        "left_identity": {"labels": ["SourceRecord"], "key": "source_record_pk", "value": "old"},
        "right_identity": {"labels": ["Person"], "key": "person_id", "value": "person"},
        "multiplicity_ordinal": 0,
    }
    inactive = {
        **base,
        "properties": {"is_active": False, "retired_by_repair_mutation_id": "prior"},
        "frozen_active": False,
    }
    current_inactive = {
        "relationship_type": "LINKED_TO",
        "left_identity": base["left_identity"],
        "right_identity": base["right_identity"],
        "properties": {"is_active": False, "retired_by_repair_mutation_id": "prior"},
        "mutation_timestamp_present": None,
    }
    assert retirement_snapshot_matches((inactive,), (current_inactive,), "current")
    active = {**base, "properties": {"is_active": True}, "frozen_active": True}
    current_without_timestamp = {
        "relationship_type": "LINKED_TO",
        "left_identity": base["left_identity"],
        "right_identity": base["right_identity"],
        "properties": {"is_active": False, "retired_by_repair_mutation_id": "current"},
        "mutation_timestamp_present": False,
    }
    assert not retirement_snapshot_matches((active,), (current_without_timestamp,), "current")


def test_run_equation_rejects_more_than_one_current_replay_attempt() -> None:
    baseline = RepairRunEquationResult(
        qualified_inventory_rows=1,
        executable_inventory_rows=1,
        negative_control_rows=0,
        applied_units=1,
        review_required_units=0,
        incomplete_units=0,
        verified_units=1,
        drifted_units=0,
        failed_units=0,
        committed_attempts=1,
        replay_no_op_attempts=0,
        active_links=1,
        unsupported_multi_links=0,
        active_deal_origin_phone_projections=0,
        active_deal_origin_email_projections=0,
        active_deal_origin_g_us_projections=0,
        reconciled_secondaries=0,
        review_required_secondaries=0,
        failed_secondaries=0,
        pending_secondaries=0,
        expected_secondary_count=0,
        observed_secondary_count=0,
        unexplained_secondary_remainder=0,
        unchanged_negative_controls=0,
        drifted_negative_controls=0,
        missing_negative_controls=0,
        stamped_negative_controls=0,
        evidence_digest=_DIGEST,
    )
    with pytest.raises(ValueError, match="bounded"):
        RepairRunEquationResult(**{**baseline.__dict__, "replay_no_op_attempts": 2})


def test_replayed_cancelled_pair_disposition_requires_exact_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.graph import crm_deal_identity_repair_verification_pair as pair_module
    from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError

    command = _command()
    persisted = [
        {
            "subject_kind": "pair_audit_case",
            "subject_stable_id": "pair-case",
            "action": "cancelled_stale_pair",
            "outcome": "reconciled",
        }
    ]

    def cancelled_snapshot(*_args: object) -> tuple[dict[str, object], ...]:
        return (
            {
                "review_case_id": "pair-case",
                "queue_state": "resolved",
                "resolution": "cancelled_stale_repair_bridge",
                "bridge_supported": False,
            },
        )

    monkeypatch.setattr(pair_module, "read_pair_snapshot", cancelled_snapshot)
    pair_module.validate_replayed_pair_dispositions(
        cast(ManagedTransaction, None), command, persisted
    )

    def non_cancellation_snapshot(*_args: object) -> tuple[dict[str, object], ...]:
        return (
            {
                "review_case_id": "pair-case",
                "queue_state": "resolved",
                "resolution": "human_resolved",
                "bridge_supported": False,
            },
        )

    monkeypatch.setattr(pair_module, "read_pair_snapshot", non_cancellation_snapshot)
    with pytest.raises(RepairVerificationDriftError, match="bridge disposition"):
        pair_module.validate_replayed_pair_dispositions(
            cast(ManagedTransaction, None), command, persisted
        )


def test_replayed_pair_bridge_only_change_drifts(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.graph import crm_deal_identity_repair_verification_pair as pair_module
    from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError

    command = _command()
    persisted = [
        {
            "subject_kind": "pair_audit_case",
            "subject_stable_id": "pair-case",
            "action": "rescored_pair",
            "outcome": "reconciled",
        }
    ]

    def bridge_removed(*_args: object) -> tuple[dict[str, object], ...]:
        return (
            {
                "review_case_id": "pair-case",
                "queue_state": "open",
                "resolution": None,
                "bridge_supported": False,
            },
        )

    monkeypatch.setattr(pair_module, "read_pair_snapshot", bridge_removed)
    with pytest.raises(RepairVerificationDriftError, match="bridge disposition"):
        pair_module.validate_replayed_pair_dispositions(
            cast(ManagedTransaction, None), command, persisted
        )
