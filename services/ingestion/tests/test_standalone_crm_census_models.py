"""Strict identity and fingerprint contracts for standalone CRM censuses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from src.standalone_crm_census_models import (
    TERMINAL_PARENT_STATES,
    AuthorityHeads,
    CensusBudgets,
    CensusIdentity,
    CensusKind,
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncCensusRequest,
    census_fingerprint,
)


def _identity(occurrence_key: str = "occurrence") -> CensusIdentity:
    return CensusIdentity(
        source_instance_id="bitrix-primary",
        control_instance_id="legacy-default",
        occurrence_key=occurrence_key,
        operator="operator",
    )


def _budget() -> CensusBudgets:
    return CensusBudgets(
        attempt_calls=10,
        occurrence_calls=20,
        attempt_rows=100,
        occurrence_rows=1000,
        attempt_runtime_seconds=60.0,
        occurrence_wall_clock_seconds=600.0,
        max_attempts=3,
    )


def test_terminal_parent_states_are_exact() -> None:
    assert {state.value for state in TERMINAL_PARENT_STATES} == {
        "completed",
        "failed",
        "cancelled_with_checkpoint",
        "freeze_failed",
    }


def test_source_sync_and_mapping_request_schemas_are_strict() -> None:
    source = SourceSyncCensusRequest(
        selected_kinds=("contact", "lead", "company"),
        configuration_digest="config",
    )
    assert source.selected_kinds == ("contact", "lead", "company")
    with pytest.raises(TypeError):
        SourceSyncCensusRequest(
            selected_kinds=("contact",),  # type: ignore[arg-type]
            configuration_digest="config",
            prepared_revision_id="unexpected",
        )
    with pytest.raises(ValueError):
        MappingPrepareCensusRequest(
            prepared_revision_id="",
            prepared_revision_digest="digest",
            expected_current_head="head",
        )


def test_fingerprints_are_kind_disjoint_and_stable() -> None:
    budget = _budget()
    source_heads = AuthorityHeads(mapping_head="mapping-1", projection_head="projection-1")
    source = SourceSyncCensusRequest(selected_kinds=("contact",), configuration_digest="config")
    prepare = MappingPrepareCensusRequest(
        prepared_revision_id="revision-1",
        prepared_revision_digest="digest-1",
        expected_current_head="head-1",
    )
    rollback = MappingRollbackCensusRequest(
        target_revision_id="revision-2",
        target_revision_digest="digest-2",
        expected_current_head="head-2",
        rollback_head="head-1",
    )
    source_hash = census_fingerprint(
        kind=CensusKind.SOURCE_SYNC,
        identity=_identity(),
        request=source,
        budget=budget,
        heads=source_heads,
    )
    prepare_hash = census_fingerprint(
        kind=CensusKind.MAPPING_PREPARE,
        identity=_identity(),
        request=prepare,
        budget=budget,
        heads=AuthorityHeads(
            prepared_revision_id=prepare.prepared_revision_id,
            prepared_revision_digest=prepare.prepared_revision_digest,
        ),
    )
    rollback_hash = census_fingerprint(
        kind=CensusKind.MAPPING_ROLLBACK,
        identity=_identity(),
        request=rollback,
        budget=budget,
        heads=AuthorityHeads(
            prepared_revision_id=rollback.target_revision_id,
            prepared_revision_digest=rollback.target_revision_digest,
            rollback_head=rollback.rollback_head,
        ),
    )
    assert source_hash != prepare_hash != rollback_hash
    assert source_hash == census_fingerprint(
        kind=CensusKind.SOURCE_SYNC,
        identity=_identity(),
        request=source,
        budget=budget,
        heads=source_heads,
    )
    with pytest.raises(ValueError):
        census_fingerprint(
            kind=CensusKind.SOURCE_SYNC,
            identity=_identity(),
            request=source,
            budget=budget,
            heads=AuthorityHeads(mapping_head="mapping-1"),
        )


def test_budgets_reject_inverted_ceilings() -> None:
    with pytest.raises(ValueError, match="attempt call ceiling exceeds the occurrence"):
        CensusBudgets(
            attempt_calls=2,
            occurrence_calls=1,
            attempt_rows=1,
            occurrence_rows=1,
            attempt_runtime_seconds=1.0,
            occurrence_wall_clock_seconds=1.0,
            max_attempts=1,
        )


def test_absolute_deadline_helpers() -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    assert now + timedelta(seconds=1) > now
