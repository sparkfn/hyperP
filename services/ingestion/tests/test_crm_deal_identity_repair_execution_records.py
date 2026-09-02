"""Structural tests for immutable future repair ledger contracts."""

from __future__ import annotations

import pytest
from src.crm_deal_identity_repair import execution_protocols
from src.crm_deal_identity_repair.execution_models import (
    RepairCheckpoint,
    RepairFence,
    RepairMutationResult,
    RepairOutboxEvent,
    RepairQuiescence,
    RepairRollbackImage,
    RepairSecondaryDisposition,
    RepairUnit,
    RepairVerificationResult,
)
from src.graph.crm_deal_identity_repair_ledger_migration import (
    REQUIRED_CONSTRAINTS,
    REQUIRED_INDEXES,
)
from src.graph.queries.crm_deal_identity_repair_ledger import (
    CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA,
)
from src.graph.schema_init import _find_init_cypher, _split_statements

DIGEST = "sha256:" + "a" * 64


def test_future_records_bind_identity_generation_attempt_and_evidence() -> None:
    unit = RepairUnit("run-1", "unit-1", 1, 0, 1, DIGEST, DIGEST, "allocated")
    checkpoint = RepairCheckpoint(
        "run-1",
        "unit-1",
        "checkpoint-1",
        1,
        1,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        "written",
    )
    fence = RepairFence(
        "run-1",
        "unit-1",
        "fence-1",
        1,
        1,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        "claimed",
    )
    mutation = RepairMutationResult(
        "run-1",
        "unit-1",
        "mutation-1",
        1,
        2,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        "applied",
    )
    rollback = RepairRollbackImage(
        "run-1",
        "unit-1",
        "image-1",
        1,
        2,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        "available",
    )

    assert unit.attempt == 1
    assert checkpoint.fence_token == fence.token
    assert mutation.rollback_image_digest == rollback.image_digest


def test_future_records_require_strict_literals_and_immutable_sequence_values() -> None:
    with pytest.raises(ValueError, match="allocation attempt"):
        RepairUnit("run-1", "unit-1", 1, 0, 0, DIGEST, DIGEST, "allocated")
    with pytest.raises(ValueError, match="checkpoint state"):
        RepairCheckpoint(
            "run-1",
            "unit-1",
            "checkpoint-1",
            1,
            0,
            1,
            "owner-1",
            "token-1",
            DIGEST,
            DIGEST,
            DIGEST,
            "open",
        )
    with pytest.raises(ValueError, match="fence state"):
        RepairFence(
            "run-1",
            "unit-1",
            "fence-1",
            1,
            0,
            1,
            "owner-1",
            "token-1",
            DIGEST,
            DIGEST,
            "open",
        )


def test_reconciliation_rollback_outbox_and_quiescence_are_fully_bound() -> None:
    secondary = RepairSecondaryDisposition(
        "run-1",
        "unit-1",
        "secondary-1",
        1,
        3,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        "reconciled",
    )
    verification = RepairVerificationResult(
        "run-1",
        "unit-1",
        "verification-1",
        1,
        4,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        DIGEST,
        "verified",
    )
    outbox = RepairOutboxEvent(
        "run-1",
        "unit-1",
        "event-1",
        1,
        5,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        "pending",
    )
    quiescence = RepairQuiescence(
        "run-1",
        "quiescence-1",
        1,
        6,
        1,
        "owner-1",
        "token-1",
        DIGEST,
        DIGEST,
        DIGEST,
        "quiesced",
    )

    assert secondary.control_token == "token-1"
    assert verification.outcome == "verified"
    assert outbox.state == "pending"
    assert quiescence.state == "quiesced"
    with pytest.raises(ValueError, match="outbox state"):
        RepairOutboxEvent(
            "run-1",
            "unit-1",
            "event-1",
            1,
            5,
            1,
            "owner-1",
            "token-1",
            DIGEST,
            DIGEST,
            DIGEST,
            "sent",
        )


def test_protocols_are_granular_and_schema_matches_record_identities() -> None:
    assert "claim_quiescence" in execution_protocols.RepairQuiescenceRepository.__dict__
    assert "reserve_unit" not in execution_protocols.RepairMutationRepository.__dict__
    assert "append_mutation_result" in execution_protocols.RepairMutationRepository.__dict__
    assert "append_verification" in execution_protocols.RepairVerificationRepository.__dict__
    assert "append_rollback_disposition" in execution_protocols.RepairRollbackRepository.__dict__
    assert "acknowledge_outbox_event" in execution_protocols.RepairIntegrationRepository.__dict__
    assert "read_acceptance_status" in execution_protocols.RepairAcceptanceStatusReader.__dict__
    assert "read_release_status" in execution_protocols.RepairAcceptanceStatusReader.__dict__

    assert REQUIRED_CONSTRAINTS["crm_deal_repair_checkpoint_unique"][1] == (
        "run_id",
        "checkpoint_id",
    )
    assert REQUIRED_CONSTRAINTS["crm_deal_repair_fence_unique"][1] == ("run_id", "fence_id")
    assert REQUIRED_CONSTRAINTS["crm_deal_repair_mutation_unique"][1] == ("run_id", "mutation_id")
    assert REQUIRED_CONSTRAINTS["crm_deal_repair_rollback_authorization_unique"] == (
        "CrmDealRepairRollbackAuthorization",
        ("run_id", "authorization_transition_id"),
    )
    assert REQUIRED_CONSTRAINTS["crm_deal_repair_quiescence_unique"][1] == (
        "run_id",
        "quiescence_id",
    )
    assert REQUIRED_INDEXES["crm_deal_repair_verification_outcome"][1][-1] == "outcome"

    dynamic_names = {statement.split()[2] for statement in CREATE_CRM_DEAL_REPAIR_LEDGER_SCHEMA}
    canonical_schema = "\n".join(_split_statements(_find_init_cypher().read_text(encoding="utf-8")))
    assert all(name in canonical_schema for name in dynamic_names)
