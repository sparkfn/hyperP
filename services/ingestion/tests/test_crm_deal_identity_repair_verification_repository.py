"""Structural safety checks for the #311 repository and parameterized Cypher."""

from __future__ import annotations

import inspect

from src.graph.crm_deal_identity_repair_verification import (
    CrmDealIdentityRepairVerificationRepository,
)
from src.graph.crm_deal_identity_repair_verification_replay import (
    _validate_replayed_person_dispositions,
    replay_acknowledged_verification,
)
from src.graph.queries import crm_deal_identity_repair_verification as queries


def test_repository_has_one_managed_write_boundary_and_no_dispatch() -> None:
    source = inspect.getsource(CrmDealIdentityRepairVerificationRepository)
    assert "execute_write" in source
    assert ".delay(" not in source
    assert "publish" not in source.lower()


def test_outbox_claim_is_pending_cas_and_acknowledges_only_after_persistence() -> None:
    assert "state: 'pending'" in queries.CLAIM_VERIFICATION_OUTBOX
    assert "verification_claim_token = $claim_token" in queries.CLAIM_VERIFICATION_OUTBOX
    assert "state = 'acknowledged'" in queries.PERSIST_VERIFICATION
    assert queries.PERSIST_VERIFICATION.index(
        "MERGE (verification"
    ) < queries.PERSIST_VERIFICATION.index("state = 'acknowledged'")
    assert "READ_EXACT_OUTBOX_STATE" in queries.__dict__
    assert "state: 'acknowledged'" in queries.READ_ACKNOWLEDGED_VERIFICATION


def test_negative_controls_are_supplied_as_parameters_not_refreshed_inventory() -> None:
    query = queries.READ_NEGATIVE_CONTROL_FULL_STATE
    assert "UNWIND $items AS item" in query
    assert "item.closure_source_record_pks" in query
    assert "properties(link)" in query
    assert "properties(projection)" in query
    assert "source {.*, observed_at: toString(source.observed_at)}" in query
    assert "[*0..2]" not in query
    assert "CrmDealRepairUnit {source_record_pk: source_record_pk}" in query
    assert "verification.source_record_pk" not in query
    assert "disposition.source_record_pk" not in query
    assert "MATCH (run:CrmDealRepairRun" in queries.READ_RUN_VERIFICATION_COUNTS
    assert "inventory_digest: $inventory_digest" in queries.READ_RUN_VERIFICATION_COUNTS


def test_bundle_guard_requires_exact_blocked_dispatch_and_source_cardinality() -> None:
    assert "control_instance_id: $control_instance_id" in queries.LOCK_AND_READ_VERIFICATION_BUNDLE
    assert "blocked: true" in queries.LOCK_AND_READ_VERIFICATION_BUNDLE
    assert "size(new_sources) AS new_source_count" in queries.LOCK_AND_READ_VERIFICATION_BUNDLE
    assert "count(dispatch) AS blocked_dispatch_count" in queries.LOCK_AND_READ_VERIFICATION_BUNDLE
    assert "(run)-[:HAS_REPAIR_MUTATION]->(result)" in queries.LOCK_AND_READ_VERIFICATION_BUNDLE
    assert "(unit)-[:HAS_REPAIR_OUTBOX]->(outbox)" in queries.LOCK_AND_READ_VERIFICATION_BUNDLE


def test_primary_query_separates_retirement_and_forbidden_projection_pk_domains() -> None:
    assert "WITH $retired_source_record_pks AS retired_source_record_pks" in (
        queries.READ_PRIMARY_POSTCONDITIONS
    )
    assert "WITH $retirement_requirements AS retirement_requirements" in (
        queries.READ_PRIMARY_POSTCONDITIONS
    )
    assert "frozen_active_count" in queries.READ_PRIMARY_POSTCONDITIONS
    assert "type(relationship) IN ['LINKED_TO', 'DESCRIBES_ADDRESS']" in (
        queries.READ_PRIMARY_POSTCONDITIONS
    )
    assert "WITH $closure_source_record_pks AS closure_source_record_pks" in (
        queries.READ_PRIMARY_POSTCONDITIONS
    )


def test_run_aggregation_binds_the_exact_frozen_source_pk_boundary() -> None:
    query = queries.READ_RUN_VERIFICATION_COUNTS
    assert "source_record_pks_json: $source_record_pks_json" in query
    assert "size([unit IN units WHERE unit.state = 'applied'])" in query
    assert "count([unit IN units" not in query


def test_acknowledged_replay_rederives_state_without_write_helpers() -> None:
    source = inspect.getsource(replay_acknowledged_verification)
    assert "read_person_states" in source
    assert "read_pair_snapshot" in source
    assert "verify_replayed_revision" in source
    assert "recompute_person_crm_deal_counts" not in source
    assert "recompute_golden_profile_from_active_authority" not in source
    assert "mark_profile_analysis_dirty" not in source
    assert "append_identity_link_revisions" not in source
    assert "build_person_details" in inspect.getsource(_validate_replayed_person_dispositions)


def test_cas_loser_can_only_route_to_exact_acknowledged_read_only_replay() -> None:
    source = inspect.getsource(CrmDealIdentityRepairVerificationRepository._verify)
    assert "READ_EXACT_OUTBOX_STATE" in source
    assert 'state["state"] == "acknowledged"' in source
    assert "replay_acknowledged_verification" in source


def test_pair_reconciliation_is_limited_to_authenticated_review_case_ids() -> None:
    assert "UNWIND $review_case_ids AS review_case_id" in queries.READ_PAIR_AUDIT_CASES
    assert "$person_ids" not in queries.READ_PAIR_AUDIT_CASES


def test_acknowledged_replay_binds_request_and_outbox_immutables() -> None:
    assert "request_digest: $request_digest" in queries.READ_ACKNOWLEDGED_VERIFICATION
    assert "verification_request_digest" in queries.READ_ACKNOWLEDGED_VERIFICATION


def test_derived_person_state_includes_every_rebuilt_golden_field() -> None:
    query = queries.READ_AFFECTED_PERSON_DERIVED_STATE
    for field in (
        "preferred_nric",
        "preferred_race_ethnicity",
        "profile_completeness_score",
    ):
        assert field in query


def test_revision_readback_requests_the_full_deterministic_identity_contract() -> None:
    query = queries.READ_IDENTITY_LINK_REVISION_CAUSE
    for field in (
        "source_system",
        "source_instance_id",
        "source_entity_type",
        "source_entity_id",
        "identity_policy_version",
        "link_status",
        "resolution_kind",
        "hyperp_person_id",
        "cause_key",
    ):
        assert field in query
