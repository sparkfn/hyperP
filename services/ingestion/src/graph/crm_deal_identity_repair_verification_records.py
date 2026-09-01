"""Strict immutable #309 bundle decoding for #311 verification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import (
    RepairCheckpoint,
    RepairMutationResult,
    RepairOutboxEvent,
    RepairRollbackImage,
)
from src.crm_deal_identity_repair.mutation_models import RepairMutationCommand, build_outbox_digest
from src.graph.crm_deal_identity_repair_mutation_records import (
    checkpoint_from_properties,
    mutation_result_from_properties,
    outbox_event_from_properties,
    rollback_image_from_properties,
)
from src.models import JsonValue

Scope = tuple[str, str, int, int, int, str, str, str]


class VerificationBundleError(RuntimeError):
    """Raised for a malformed, substituted, or cross-bound immutable bundle."""


@dataclass(frozen=True)
class VerificationBundle:
    result: RepairMutationResult
    image: RepairRollbackImage
    checkpoint: RepairCheckpoint
    outbox: RepairOutboxEvent
    replacement_pk: str


def decode_verification_bundle(
    command: RepairMutationCommand,
    result_values: Mapping[str, JsonValue],
    image_values: Mapping[str, JsonValue],
    checkpoint_values: Mapping[str, JsonValue],
    outbox_values: Mapping[str, JsonValue],
    replacement_pks: tuple[str, ...],
    source_count: int,
    blocked_dispatch_count: int,
) -> VerificationBundle:
    """Decode and bind the immutable #309 result, image, checkpoint, and outbox."""
    _validate_replacement_cardinality(replacement_pks, source_count)
    if blocked_dispatch_count != 1:
        raise VerificationBundleError("repair dispatch guard cardinality differs")
    result, image, checkpoint, outbox = _decode_records(
        result_values, image_values, checkpoint_values, outbox_values
    )
    _validate_scope(command, result, image, checkpoint, outbox)
    _validate_ids(command, result_values, result, image, checkpoint, outbox, replacement_pks[0])
    _validate_digests(command, result_values, result, image, checkpoint, outbox)
    _validate_outcome(command, result)
    return VerificationBundle(result, image, checkpoint, outbox, replacement_pks[0])


def _validate_replacement_cardinality(replacement_pks: tuple[str, ...], source_count: int) -> None:
    if source_count != 1 or len(replacement_pks) != 1 or not replacement_pks[0]:
        raise VerificationBundleError("repair replacement source cardinality differs")


def _decode_records(
    result_values: Mapping[str, JsonValue],
    image_values: Mapping[str, JsonValue],
    checkpoint_values: Mapping[str, JsonValue],
    outbox_values: Mapping[str, JsonValue],
) -> tuple[RepairMutationResult, RepairRollbackImage, RepairCheckpoint, RepairOutboxEvent]:
    try:
        return (
            mutation_result_from_properties(result_values),
            rollback_image_from_properties(image_values),
            checkpoint_from_properties(checkpoint_values),
            outbox_event_from_properties(outbox_values),
        )
    except (KeyError, RuntimeError, ValueError) as exc:
        raise VerificationBundleError("repair bundle record is malformed") from exc


def _validate_scope(
    command: RepairMutationCommand,
    result: RepairMutationResult,
    image: RepairRollbackImage,
    checkpoint: RepairCheckpoint,
    outbox: RepairOutboxEvent,
) -> None:
    expected = _scope(command)
    records = (
        _scope_from_result(result),
        _scope_from_image(image),
        _scope_from_checkpoint(checkpoint),
        _scope_from_outbox(outbox),
    )
    if any(record != expected for record in records):
        raise VerificationBundleError("repair bundle scope or authority differs")


def _scope(command: RepairMutationCommand) -> Scope:
    return (
        command.unit.run_id,
        command.unit.unit_id,
        command.unit.generation,
        command.unit.sequence,
        command.unit.attempt,
        command.unit.boundary_digest,
        command.fence.owner_id,
        command.fence.token,
    )


def _scope_from_result(record: RepairMutationResult) -> Scope:
    return (
        record.run_id,
        record.unit_id,
        record.generation,
        record.sequence,
        record.attempt,
        record.boundary_digest,
        record.owner_id,
        record.fence_token,
    )


def _scope_from_image(record: RepairRollbackImage) -> Scope:
    return (
        record.run_id,
        record.unit_id,
        record.generation,
        record.sequence,
        record.attempt,
        record.boundary_digest,
        record.owner_id,
        record.fence_token,
    )


def _scope_from_checkpoint(record: RepairCheckpoint) -> Scope:
    return (
        record.run_id,
        record.unit_id,
        record.generation,
        record.sequence,
        record.attempt,
        record.boundary_digest,
        record.owner_id,
        record.fence_token,
    )


def _scope_from_outbox(record: RepairOutboxEvent) -> Scope:
    return (
        record.run_id,
        record.unit_id,
        record.generation,
        record.sequence,
        record.attempt,
        record.boundary_digest,
        record.owner_id,
        record.delivery_token,
    )


def _validate_ids(
    command: RepairMutationCommand,
    result_values: Mapping[str, JsonValue],
    result: RepairMutationResult,
    image: RepairRollbackImage,
    checkpoint: RepairCheckpoint,
    outbox: RepairOutboxEvent,
    replacement_pk: str,
) -> None:
    expected_ids = (
        command.mutation_id,
        command.rollback_image_id,
        command.checkpoint_id,
        command.outbox_event_id,
    )
    observed_ids = (
        result.mutation_id,
        image.rollback_image_id,
        checkpoint.checkpoint_id,
        outbox.event_id,
    )
    if observed_ids != expected_ids:
        raise VerificationBundleError("repair bundle child identity differs")
    if _required_string(result_values, "rollback_image_id") != image.rollback_image_id:
        raise VerificationBundleError("repair result rollback image ID differs")
    if _required_string(result_values, "checkpoint_id") != checkpoint.checkpoint_id:
        raise VerificationBundleError("repair result checkpoint ID differs")
    if _required_string(result_values, "outbox_event_id") != outbox.event_id:
        raise VerificationBundleError("repair result outbox ID differs")
    if _required_string(result_values, "new_source_record_pk") != replacement_pk:
        raise VerificationBundleError("repair replacement source differs")
    if result.unit_fingerprint != command.unit.inventory_fingerprint:
        raise VerificationBundleError("repair mutation unit fingerprint differs")


def _validate_digests(
    command: RepairMutationCommand,
    result_values: Mapping[str, JsonValue],
    result: RepairMutationResult,
    image: RepairRollbackImage,
    checkpoint: RepairCheckpoint,
    outbox: RepairOutboxEvent,
) -> None:
    image_digests_match = (
        result.rollback_image_digest == image.image_digest
        and result.payload_digest == image.payload_digest
    )
    if not image_digests_match:
        raise VerificationBundleError("repair image digest differs")
    evidence_digests_match = (
        result.evidence_digest == image.evidence_digest
        and result.evidence_digest == checkpoint.evidence_digest
    )
    if not evidence_digests_match:
        raise VerificationBundleError("repair bundle evidence digest differs")
    if _required_digest(result_values, "repaired_state_digest") != image.expected_repaired_digest:
        raise VerificationBundleError("repair repaired-state digest differs")
    if _required_digest(result_values, "request_digest") != command.request_digest:
        raise VerificationBundleError("repair mutation request digest differs")
    expected_checkpoint = object_digest(
        b"crm-deal-identity-repair-checkpoint-v1\x00", {"result_digest": result.result_digest}
    )
    if checkpoint.checkpoint_digest != expected_checkpoint:
        raise VerificationBundleError("repair checkpoint digest differs")
    if outbox.payload_digest != build_outbox_digest(command, result.result_digest):
        raise VerificationBundleError("repair outbox digest differs")
    if outbox.evidence_digest != result.evidence_digest:
        raise VerificationBundleError("repair outbox evidence digest differs")


def _validate_outcome(command: RepairMutationCommand, result: RepairMutationResult) -> None:
    if result.outcome not in {"applied", "review_required"}:
        raise VerificationBundleError("repair mutation outcome is not verifiable")
    if command.unit.state not in {"allocated", "quiesced", result.outcome}:
        raise VerificationBundleError("repair unit state is incompatible with mutation outcome")


def _required_string(values: Mapping[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise VerificationBundleError("repair bundle property is invalid")
    return value


def _required_digest(values: Mapping[str, JsonValue], key: str) -> str:
    value = _required_string(values, key)
    if not value.startswith("sha256:") or len(value) != 71:
        raise VerificationBundleError("repair bundle digest is invalid")
    if any(char not in "0123456789abcdef" for char in value[7:]):
        raise VerificationBundleError("repair bundle digest is invalid")
    return value
