"""Structural regression coverage for durable Bitrix deal-scope boundaries."""

from __future__ import annotations

from inspect import signature

from pytest import raises
from src.graph.bitrix_deal_scope import (
    MAX_DEAL_SCOPE_BATCH_SIZE,
    BitrixDealScopeRepository,
    DealScopeLookup,
    DealScopeObservation,
)
from src.graph.queries.bitrix_deal_scope import (
    CREATE_BITRIX_DEAL_SCOPE_CONSTRAINTS,
    GET_CURRENT_DEAL_SCOPE_BATCH,
    MIGRATE_BITRIX_DEAL_SCOPE_LINEAGE_CONSTRAINTS,
    UPSERT_DEAL_SCOPE_MEMBERSHIPS,
)


def test_scope_observation_requires_an_explicit_safe_state_shape() -> None:
    observation = DealScopeObservation(
        deal_id="deal-1",
        scope_state="in_scope",
        category_id="7",
        entity_key="entity-1",
        source_record_pk="source-1",
    )

    assert observation.scope_state == "in_scope"

    with raises(ValueError, match="category_id and entity_key"):
        DealScopeObservation(deal_id="deal-2", scope_state="in_scope", category_id="7")
    with raises(ValueError, match="Only in-scope"):
        DealScopeObservation(
            deal_id="deal-3",
            scope_state="out_of_scope",
            entity_key="entity-1",
        )


def test_lookup_requires_missing_or_the_exact_durable_scope_state() -> None:
    missing = DealScopeLookup(deal_id="missing-deal", state="missing", current=None)

    assert missing.state == "missing"

    with raises(ValueError, match="cannot have a scope state"):
        DealScopeLookup(deal_id="missing-deal", state="in_scope", current=None)


def test_batch_api_is_bounded_and_reserves_a_fence_shape() -> None:
    record_batch = signature(BitrixDealScopeRepository.record_batch)
    lookup_batch = signature(BitrixDealScopeRepository.get_current_batch)

    assert MAX_DEAL_SCOPE_BATCH_SIZE == 250
    assert "fence_context" in record_batch.parameters
    assert "fence_context" in lookup_batch.parameters
    assert record_batch.parameters["fence_context"].default is None
    assert lookup_batch.parameters["fence_context"].default is None


def test_scope_lineage_is_unique_per_logical_deal_sequence() -> None:
    active_schema = "\n".join(CREATE_BITRIX_DEAL_SCOPE_CONSTRAINTS)
    migration_schema = "\n".join(MIGRATE_BITRIX_DEAL_SCOPE_LINEAGE_CONSTRAINTS)

    assert "crm_deal_scope_membership_identity_unique" in active_schema
    assert "DROP CONSTRAINT" not in active_schema
    assert "crm_deal_scope_lineage_identity_unique" in migration_schema
    lineage_identity = (
        "(membership.source_key, membership.deal_id, membership.scope_sequence) IS UNIQUE"
    )
    assert lineage_identity in migration_schema
    assert "DROP CONSTRAINT crm_deal_scope_membership_identity_unique IF EXISTS" in migration_schema


def test_batch_write_uses_a_semantic_change_to_append_immutable_lineage() -> None:
    before_current_update, after_current_update = UPSERT_DEAL_SCOPE_MEMBERSHIPS.split(
        "SET deal.current_scope_sequence", maxsplit=1
    )

    assert "UNWIND $observations AS observation" in UPSERT_DEAL_SCOPE_MEMBERSHIPS
    assert "deal.current_scope_state <> observation.scope_state" in before_current_update
    assert "deal.current_entity_key" in before_current_update
    assert "deal.current_category_id" in before_current_update
    assert "source_record_pk" not in before_current_update
    assert "MERGE (membership:CrmDealScopeMembership" in after_current_update
    assert "scope_sequence: deal.current_scope_sequence" in after_current_update
    assert "ON CREATE SET membership.membership_id" in after_current_update
    assert after_current_update.count("SET membership.") == 1
    assert "DETACH DELETE" not in UPSERT_DEAL_SCOPE_MEMBERSHIPS


def test_batch_lookup_returns_every_requested_deal_with_an_optional_match() -> None:
    assert "UNWIND $deal_ids AS requested_deal_id" in GET_CURRENT_DEAL_SCOPE_BATCH
    assert "OPTIONAL MATCH (deal:CrmLogicalDeal" in GET_CURRENT_DEAL_SCOPE_BATCH
    assert "requested_deal_id AS deal_id" in GET_CURRENT_DEAL_SCOPE_BATCH
    assert "deal.current_scope_state AS scope_state" in GET_CURRENT_DEAL_SCOPE_BATCH
