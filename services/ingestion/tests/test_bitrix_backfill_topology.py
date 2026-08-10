"""Corrective-generation topology and checkpoint contract tests."""

from src.bitrix_backfill_models import (
    KnownOwnerMembershipSet,
    initial_stream_checkpoint,
    known_owner_refresh_checkpoint,
)
from src.graph.queries.bitrix_backfill import (
    ATTACH_BACKFILL_LOGICAL_RUN,
    CREATE_BITRIX_BACKFILL_CONSTRAINTS,
    GET_MAX_BITRIX_RESUME_WORKER_GENERATION,
    MATERIALIZE_KNOWN_OWNER_SET,
)


def test_stream_checkpoint_schemas_have_fixed_restart_boundaries() -> None:
    deals = initial_stream_checkpoint(
        "crm_deals",
        source_window={
            "upper_deal_id": "900",
            "included_category_digest": "sha256:categories",
            "owner_artifact_id": None,
        },
    )
    activities = initial_stream_checkpoint(
        "crm_activities",
        source_window={"upper_activity_id": "1200", "owner_artifact_id": None},
    )
    openlines = initial_stream_checkpoint(
        "openlines_conversations",
        source_window={
            "discovery_boundary_digest": "sha256:discovery",
            "selected_config_digest": "sha256:config",
        },
    )

    assert deals.phase == "scoped_deal_census_v1"
    assert deals.cursor == {"last_deal_id": None, "census_epoch": 1}
    assert deals.replay_boundary == "exclusive_last_deal_id"
    assert activities.phase == "crm_activity_keyset_v1"
    assert activities.replay_boundary == "exclusive_last_activity_id"
    assert openlines.phase == "openlines_conversation_replay_v1"
    assert openlines.replay_boundary == "at_least_once_page_start"


def test_known_owner_refresh_binds_the_sealed_membership_set() -> None:
    membership = KnownOwnerMembershipSet(
        generation_id="generation-1",
        membership_set_id="owners-1",
        digest="sha256:owners",
        deal_ids=("2", "10"),
    )

    checkpoint = known_owner_refresh_checkpoint(membership, census_epoch=3)

    assert checkpoint.phase == "known_owner_refresh_v1"
    assert checkpoint.cursor == {"last_known_deal_id": None, "census_epoch": 3}
    assert checkpoint.source_window["known_owner_count"] == 2
    assert checkpoint.source_window["known_owner_set_digest"] == "sha256:owners"


def test_generation_topology_is_unique_and_child_runs_are_explicit() -> None:
    schema = "\n".join(CREATE_BITRIX_BACKFILL_CONSTRAINTS)

    assert "BitrixBackfillGeneration" in schema
    assert "BitrixKnownOwnerRefreshMember" in schema
    assert "BitrixBackfillCoverage" in schema
    assert "HAS_LOGICAL_RUN" in ATTACH_BACKFILL_LOGICAL_RUN
    assert "HAS_STREAM" in ATTACH_BACKFILL_LOGICAL_RUN
    assert "current_ids = $deal_ids" in MATERIALIZE_KNOWN_OWNER_SET
    assert "sealed_at" in MATERIALIZE_KNOWN_OWNER_SET
    assert "DELETE" not in MATERIALIZE_KNOWN_OWNER_SET


def test_resume_worker_generation_uses_durable_attempt_history() -> None:
    assert "HAS_ATTEMPT" in GET_MAX_BITRIX_RESUME_WORKER_GENERATION
    assert ":resume:" in GET_MAX_BITRIX_RESUME_WORKER_GENERATION
    assert "max(toInteger" in GET_MAX_BITRIX_RESUME_WORKER_GENERATION
    assert "coalesce" in GET_MAX_BITRIX_RESUME_WORKER_GENERATION
