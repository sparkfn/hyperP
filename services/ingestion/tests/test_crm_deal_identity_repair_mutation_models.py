"""Pure contracts for the #309 atomic mutation command and digest bindings."""

from __future__ import annotations

import pytest
from src.crm_deal_identity_repair.execution_models import RepairFence, RepairUnit
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.crm_deal_identity_repair.mutation_models import (
    RepairMutationCommand,
    build_inventory_binding_digest,
)

DIGEST = "sha256:" + "a" * 64


def _inventory(partition: RepairPartition = "ownership_repair") -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-1",
        source_record_pk="deal-pk",
        deal_id="1",
        partition=partition,
        graph_fingerprint=DIGEST,
        stored_payload_fingerprint=DIGEST,
        payload={},
    )


def _unit(inventory: RepairInventoryItem) -> RepairUnit:
    return RepairUnit(
        "run",
        "unit",
        1,
        0,
        1,
        DIGEST,
        inventory.graph_fingerprint,
        "allocated",
        inventory.inventory_key,
        inventory.source_record_pk,
        inventory.graph_fingerprint,
        inventory.stored_payload_fingerprint,
        build_inventory_binding_digest(inventory),
    )


def _command() -> RepairMutationCommand:
    inventory = _inventory()
    return RepairMutationCommand(
        _unit(inventory),
        RepairFence("run", "unit", "fence", 1, 0, 1, "owner", "token", DIGEST, DIGEST, "claimed"),
        inventory,
        "source-instance",
        "control-instance",
    )


def test_mutation_command_derives_stable_ledger_identities_from_all_bound_evidence() -> None:
    command = _command()

    assert command.mutation_id == _command().mutation_id
    assert command.rollback_image_id != command.checkpoint_id
    assert command.checkpoint_id != command.outbox_event_id
    assert command.request_digest.startswith("sha256:")


def test_negative_controls_and_lost_fences_are_rejected_before_transaction_work() -> None:
    inventory = _inventory()
    unit = _unit(inventory)
    fence = RepairFence("run", "unit", "fence", 1, 0, 1, "owner", "token", DIGEST, DIGEST, "lost")
    with pytest.raises(ValueError, match="claimed fence"):
        RepairMutationCommand(unit, fence, inventory, "source-instance", "control-instance")
    with pytest.raises(ValueError, match="negative-control"):
        RepairMutationCommand(
            unit,
            RepairFence(
                "run", "unit", "fence", 1, 0, 1, "owner", "token", DIGEST, DIGEST, "claimed"
            ),
            _inventory("negative_control"),
            "source-instance",
            "control-instance",
        )
