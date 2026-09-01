"""Strict #309 immutable-bundle decoding coverage for #311."""

from __future__ import annotations

import pytest
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import RepairFence, RepairUnit
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_models import (
    build_inventory_binding_digest,
    build_outbox_digest,
)
from src.crm_deal_identity_repair.verification_models import RepairVerificationCommand
from src.graph.crm_deal_identity_repair_verification_records import (
    VerificationBundleError,
    decode_verification_bundle,
)
from src.models import JsonValue

_DIGEST = "sha256:" + "1" * 64


def _command() -> RepairVerificationCommand:
    item = RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-7",
        source_record_pk="old-pk",
        deal_id="7",
        partition="ownership_repair",
        graph_fingerprint=_DIGEST,
        stored_payload_fingerprint=_DIGEST,
        payload={"descendants": []},
    )
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


def _bundle(command: RepairVerificationCommand) -> tuple[dict[str, JsonValue], ...]:
    result_digest = "sha256:" + "2" * 64
    checkpoint_digest = object_digest(
        b"crm-deal-identity-repair-checkpoint-v1\x00", {"result_digest": result_digest}
    )
    result: dict[str, JsonValue] = {
        "run_id": "run",
        "unit_id": "unit",
        "mutation_id": command.mutation_id,
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "owner_id": "owner",
        "fence_token": "token",
        "boundary_digest": _DIGEST,
        "unit_fingerprint": _DIGEST,
        "result_digest": result_digest,
        "rollback_image_id": command.rollback_image_id,
        "new_source_record_pk": "new-pk",
        "rollback_image_digest": _DIGEST,
        "evidence_digest": _DIGEST,
        "payload_digest": _DIGEST,
        "outcome": "applied",
        "request_digest": command.mutation_command.request_digest,
        "repaired_state_digest": _DIGEST,
        "checkpoint_id": command.checkpoint_id,
        "outbox_event_id": command.outbox_event_id,
    }
    image: dict[str, JsonValue] = {
        "run_id": "run",
        "unit_id": "unit",
        "rollback_image_id": command.rollback_image_id,
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "owner_id": "owner",
        "fence_token": "token",
        "boundary_digest": _DIGEST,
        "source_fingerprint": _DIGEST,
        "image_digest": _DIGEST,
        "expected_repaired_digest": _DIGEST,
        "evidence_digest": _DIGEST,
        "payload_digest": _DIGEST,
        "state": "available",
    }
    checkpoint: dict[str, JsonValue] = {
        "run_id": "run",
        "unit_id": "unit",
        "checkpoint_id": command.checkpoint_id,
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "owner_id": "owner",
        "fence_token": "token",
        "boundary_digest": _DIGEST,
        "checkpoint_digest": checkpoint_digest,
        "evidence_digest": _DIGEST,
    }
    outbox: dict[str, JsonValue] = {
        "run_id": "run",
        "unit_id": "unit",
        "event_id": command.outbox_event_id,
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "owner_id": "owner",
        "delivery_token": "token",
        "boundary_digest": _DIGEST,
        "payload_digest": build_outbox_digest(command.mutation_command, result_digest),
        "evidence_digest": _DIGEST,
        "state": "pending",
    }
    return result, image, checkpoint, outbox


def _decode(command: RepairVerificationCommand, bundle: tuple[dict[str, JsonValue], ...]) -> None:
    decode_verification_bundle(command.mutation_command, *bundle, ("new-pk",), 1, 1)


def test_decodes_a_complete_exact_bundle() -> None:
    _decode(_command(), _bundle(_command()))


@pytest.mark.parametrize(
    "record_index,key,value",
    [
        (0, "owner_id", "other"),
        (0, "fence_token", "other"),
        (0, "boundary_digest", "sha256:" + "3" * 64),
        (0, "generation", 2),
        (0, "rollback_image_digest", "sha256:" + "3" * 64),
        (0, "request_digest", "sha256:" + "3" * 64),
        (0, "new_source_record_pk", "other-pk"),
        (2, "checkpoint_digest", "sha256:" + "3" * 64),
        (3, "payload_digest", "sha256:" + "3" * 64),
        (3, "delivery_token", "other"),
    ],
)
def test_rejects_bundle_tamper(record_index: int, key: str, value: JsonValue) -> None:
    command = _command()
    rows = [dict(row) for row in _bundle(command)]
    rows[record_index][key] = value
    with pytest.raises(VerificationBundleError):
        _decode(command, tuple(rows))


def test_rejects_same_cardinality_substituted_child_id() -> None:
    command = _command()
    rows = [dict(row) for row in _bundle(command)]
    rows[1]["rollback_image_id"] = "different-image"
    with pytest.raises(VerificationBundleError):
        _decode(command, tuple(rows))


@pytest.mark.parametrize(
    "replacement_pks,source_count",
    [(("new-pk", "other"), 2), ((), 0)],
)
def test_rejects_replacement_cardinality(
    replacement_pks: tuple[str, ...], source_count: int
) -> None:
    command = _command()
    with pytest.raises(VerificationBundleError):
        decode_verification_bundle(
            command.mutation_command, *_bundle(command), replacement_pks, source_count, 1
        )


@pytest.mark.parametrize("blocked_dispatch_count", (0, 2))
def test_rejects_missing_or_duplicate_blocked_dispatch(blocked_dispatch_count: int) -> None:
    command = _command()
    with pytest.raises(VerificationBundleError):
        decode_verification_bundle(
            command.mutation_command,
            *_bundle(command),
            ("new-pk",),
            1,
            blocked_dispatch_count,
        )


def test_rejects_malformed_property() -> None:
    command = _command()
    rows = [dict(row) for row in _bundle(command)]
    del rows[0]["mutation_id"]
    with pytest.raises(VerificationBundleError):
        _decode(command, tuple(rows))
