"""Immutable ledger, terminal, replay, and status validation for #312 rollback."""

from __future__ import annotations

from dataclasses import replace
from typing import Literal, TypedDict, cast

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import (
    mutation_request_digest,
    mutation_result_digest,
    object_digest,
    outbox_event_digest,
    repaired_state_digest,
    rollback_request_digest,
)
from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackAuthorization,
    RepairRollbackCommand,
    RepairRollbackDrift,
    RepairRollbackResult,
    RepairRollbackStatus,
    build_rollback_result_digest,
    build_rollback_status_digest,
)
from src.graph.crm_deal_identity_repair_rollback_image import (
    RollbackImageBundle,
    decode_rollback_image,
)
from src.graph.crm_deal_identity_repair_rollback_records import (
    RepairRollbackRecordError,
    disposition_from_properties,
    drift_from_properties,
    drift_summaries_json,
    fence_from_properties,
    image_from_properties,
    mutation_from_properties,
    payload_json,
    property_map,
    unit_from_properties,
)
from src.graph.queries.crm_deal_identity_repair_rollback import (
    PERSIST_ROLLBACK_TERMINAL,
    READ_ROLLBACK_TERMINAL,
)
from src.models import JsonValue


class RepairRollbackAuthorityError(RuntimeError):
    """A stale or foreign transition was rejected without any domain/ledger evidence."""


class RepairRollbackDriftError(RuntimeError):
    """Immutable ledger corruption or a failed rollback invariant."""


_ImageState = Literal["available", "restored", "review_required"]
_TerminalDecision = Literal["restored", "reviewed_compensation_required"]


class _GuardParameters(TypedDict):
    run_id: str
    unit_id: str
    generation: int
    sequence: int
    attempt: int
    boundary_digest: str
    fence_id: str
    owner_id: str
    fence_token: str
    mutation_id: str
    rollback_image_id: str
    image_digest: str
    rollback_request_digest: str
    authorization_reference: str
    authorization_token: str
    predecessor_transition_id: str
    authorization_policy: str
    authorization_transition_id: str
    original_source_record_pk: str
    unit_fingerprint: str


class _PersistParameters(_GuardParameters):
    disposition_id: str
    evidence_digest: str
    payload_digest: str
    result_digest: str
    image_state: str
    unit_state: str
    outcome: str
    drift_total_mismatch_count: int
    drift_summaries_json: str
    drift_complete_digest: str | None
    status_digest: str


class RollbackLedgerMixin:
    """Repository mixin for immutable bundle and terminal evidence handling."""

    def _read_status(
        self, tx: ManagedTransaction, command: RepairRollbackCommand
    ) -> RepairRollbackStatus:
        row = tx.run(READ_ROLLBACK_TERMINAL, **self._terminal_params(command)).single()
        if row is None:
            raise RepairRollbackAuthorityError("rollback image is absent")
        authorization, payload = self._read_immutable_bundle(row, command)
        bundle = decode_rollback_image(
            authorization.image,
            authorization.mutation,
            payload,
            _result_request_digest(row),
        )
        self._assert_decoded_bundle_bindings(row, authorization, bundle)
        image = property_map(row["image"], "image")
        terminal_values = _optional_singleton(row["dispositions"], "disposition")
        state = image.get("state")
        terminal = terminal_values
        if state not in {"available", "restored", "review_required"}:
            raise RepairRollbackDriftError("rollback image state is malformed")
        terminal_id = None if terminal is None else _required_string(terminal, "disposition_id")
        if state == "available" and terminal_id is not None:
            raise RepairRollbackDriftError("available image has terminal disposition")
        if state != "available" and terminal_id is None:
            raise RepairRollbackDriftError("terminal image lacks disposition")
        if state == "available":
            self._assert_persisted_authorization(row, authorization, terminal=False)
            digest = build_rollback_status_digest(command, "available", None, None, None, None)
            return RepairRollbackStatus("available", None, digest)
        replay = self._terminal_from_row(row, command, authorization)
        if replay is None:
            raise RepairRollbackDriftError("terminal rollback state is incomplete")
        return RepairRollbackStatus(
            replay.image_state,
            terminal_id,
            build_rollback_status_digest(
                command,
                replay.image_state,
                terminal_id,
                replay.original_terminal_decision,
                replay.result_digest,
                replay.drift,
            ),
        )

    def _terminal(
        self, tx: ManagedTransaction, command: RepairRollbackCommand
    ) -> RepairRollbackResult | None:
        row = tx.run(READ_ROLLBACK_TERMINAL, **self._terminal_params(command)).single()
        if row is None or row["image"] is None:
            return None
        authorization, payload = self._read_immutable_bundle(row, command)
        bundle = decode_rollback_image(
            authorization.image,
            authorization.mutation,
            payload,
            _result_request_digest(row),
        )
        self._assert_decoded_bundle_bindings(row, authorization, bundle)
        return self._terminal_from_row(row, command, authorization)

    def _terminal_from_row(
        self,
        row: Record,
        command: RepairRollbackCommand,
        authorization: RepairRollbackAuthorization,
    ) -> RepairRollbackResult | None:
        image = property_map(row["image"], "image")
        state = image.get("state")
        if state == "available":
            return None
        disposition_values = _require_singleton(row["dispositions"], "disposition")
        self._assert_persisted_authorization(row, authorization, terminal=True)
        disposition = disposition_from_properties(disposition_values)
        if disposition.disposition_id != command.disposition_id:
            raise RepairRollbackAuthorityError("rollback image was consumed by another transition")
        original: _TerminalDecision = (
            "restored" if state == "restored" else "reviewed_compensation_required"
        )
        expected_outcome = "reconciled" if original == "restored" else "review_required"
        if disposition.outcome != expected_outcome:
            raise RepairRollbackDriftError("rollback terminal disposition outcome differs")
        drift = drift_from_properties(
            disposition_values, required=original == "reviewed_compensation_required"
        )
        self._assert_terminal_disposition(
            disposition_values, command, authorization, expected_outcome
        )
        digest = _required_string(image, "rollback_result_digest")
        if _required_string(disposition_values, "result_digest") != digest:
            raise RepairRollbackDriftError("rollback terminal result digest differs")
        expected_digest = build_rollback_result_digest(
            command, original, cast(_ImageState, state), drift
        )
        if digest != expected_digest:
            raise RepairRollbackDriftError("rollback terminal result digest is invalid")
        status_digest = build_rollback_status_digest(
            command,
            cast(_ImageState, state),
            disposition.disposition_id,
            original,
            digest,
            drift,
        )
        if _required_string(image, "rollback_status_digest") != status_digest:
            raise RepairRollbackDriftError("rollback terminal status digest is invalid")
        if _required_string(disposition_values, "rollback_status_digest") != status_digest:
            raise RepairRollbackDriftError("rollback disposition status digest is invalid")
        return RepairRollbackResult(
            "replayed",
            cast(_ImageState, state),
            digest,
            disposition,
            drift,
            original_terminal_decision=original,
        )

    def _authorization_from_row(
        self, row: Record, command: RepairRollbackCommand
    ) -> tuple[RepairRollbackAuthorization, str]:
        stored_unit = unit_from_properties(property_map(row["unit"], "unit"))
        fence = fence_from_properties(property_map(row["fence"], "fence"))
        mutation = mutation_from_properties(property_map(row["result"], "result"))
        image_values = property_map(row["image"], "image")
        image = image_from_properties(image_values)
        requested = command.authorization
        if image.state == "available" and fence.state != "claimed":
            raise RepairRollbackAuthorityError("rollback fence is no longer claimed")
        # Unit and fence lifecycle state are mutable terminal markers.  Their
        # immutable identities remain graph-derived and must match the original
        # command before replay reconstructs the consumable authority shape.
        if replace(fence, state=requested.fence.state) != requested.fence:
            raise RepairRollbackDriftError("rollback immutable fence differs")
        unit = replace(stored_unit, state=requested.unit.state)
        authority_fence = replace(fence, state=requested.fence.state)
        return (
            RepairRollbackAuthorization(
                unit,
                authority_fence,
                mutation,
                image,
                requested.authorization_reference,
                requested.authorization_token,
                requested.predecessor_transition_id,
                requested.authorization_policy,
                requested.authorization_transition_id,
            ),
            payload_json(image_values),
        )

    def _read_immutable_bundle(
        self, row: Record, command: RepairRollbackCommand
    ) -> tuple[RepairRollbackAuthorization, str]:
        try:
            authorization, image_payload = self._authorization_from_row(row, command)
            if authorization.to_dict() != command.authorization.to_dict():
                raise RepairRollbackAuthorityError("rollback authority changed before lock")
            self._assert_bundle_cross_records(row, authorization)
        except (RepairRollbackRecordError, ValueError) as exc:
            raise RepairRollbackDriftError("rollback immutable bundle is malformed") from exc
        return authorization, image_payload

    def _persist_terminal(
        self,
        tx: ManagedTransaction,
        command: RepairRollbackCommand,
        decision: _TerminalDecision,
        drift: RepairRollbackDrift | None,
    ) -> RepairRollbackResult:
        image_state: _ImageState = "restored" if decision == "restored" else "review_required"
        outcome = "reconciled" if decision == "restored" else "review_required"
        result_digest = build_rollback_result_digest(command, decision, image_state, drift)
        row = tx.run(
            PERSIST_ROLLBACK_TERMINAL,
            **self._persist_params(command, image_state, outcome, result_digest, drift),
        ).single()
        if row is None:
            raise RepairRollbackAuthorityError("rollback terminal CAS rejected")
        disposition_values = property_map(row["disposition"], "disposition")
        persisted_authorization = property_map(row["authorization"], "authorization")
        disposition = disposition_from_properties(disposition_values)
        self._assert_terminal_disposition(
            disposition_values, command, command.authorization, outcome
        )
        persisted_drift = drift_from_properties(
            disposition_values, required=decision == "reviewed_compensation_required"
        )
        if persisted_drift != drift:
            raise RepairRollbackDriftError("persisted rollback drift differs")
        if _required_string(disposition_values, "result_digest") != result_digest:
            raise RepairRollbackDriftError("persisted rollback result digest differs")
        expected_status_digest = build_rollback_status_digest(
            command,
            image_state,
            disposition.disposition_id,
            decision,
            result_digest,
            drift,
        )
        if _required_string(disposition_values, "rollback_status_digest") != expected_status_digest:
            raise RepairRollbackDriftError("persisted rollback status digest differs")
        self._assert_consumed_authorization(
            persisted_authorization, command.authorization, command.disposition_id, result_digest
        )
        return RepairRollbackResult(decision, image_state, result_digest, disposition, drift)

    def _params(self, command: RepairRollbackCommand) -> _GuardParameters:
        auth = command.authorization
        return {
            "run_id": auth.unit.run_id,
            "unit_id": auth.unit.unit_id,
            "generation": auth.unit.generation,
            "sequence": auth.unit.sequence,
            "attempt": auth.unit.attempt,
            "boundary_digest": auth.unit.boundary_digest,
            "fence_id": auth.fence.fence_id,
            "owner_id": auth.fence.owner_id,
            "fence_token": auth.fence.token,
            "mutation_id": auth.mutation.mutation_id,
            "rollback_image_id": auth.image.rollback_image_id,
            "image_digest": auth.image.image_digest,
            "rollback_request_digest": command.request_digest,
            "authorization_reference": auth.authorization_reference,
            "authorization_token": auth.authorization_token,
            "predecessor_transition_id": auth.predecessor_transition_id,
            "authorization_policy": auth.authorization_policy,
            "authorization_transition_id": auth.authorization_transition_id,
            "original_source_record_pk": "",
            "unit_fingerprint": auth.unit.inventory_fingerprint,
        }

    def _terminal_params(self, command: RepairRollbackCommand) -> _GuardParameters:
        return self._params(command)

    def _persist_params(
        self,
        command: RepairRollbackCommand,
        image_state: str,
        outcome: str,
        result_digest: str,
        drift: RepairRollbackDrift | None,
    ) -> _PersistParameters:
        guard = self._params(command)
        auth = command.authorization
        return {
            **guard,
            "image_digest": auth.image.image_digest,
            "disposition_id": command.disposition_id,
            "evidence_digest": auth.image.evidence_digest,
            "payload_digest": command.request_digest,
            "result_digest": result_digest,
            "image_state": image_state,
            "unit_state": "rolled_back" if image_state == "restored" else "review_required",
            "outcome": outcome,
            "drift_total_mismatch_count": 0 if drift is None else drift.total_mismatch_count,
            "drift_summaries_json": drift_summaries_json(())
            if drift is None
            else drift_summaries_json(drift.summaries),
            "drift_complete_digest": None if drift is None else drift.complete_digest,
            "status_digest": build_rollback_status_digest(
                command,
                cast(_ImageState, image_state),
                command.disposition_id,
                "restored" if image_state == "restored" else "reviewed_compensation_required",
                result_digest,
                drift,
            ),
        }

    def _assert_bundle_cross_records(
        self, row: Record, authorization: RepairRollbackAuthorization
    ) -> None:
        """Validate cardinality and all immutable stage-one record cross-links."""
        checkpoint = _require_singleton(_record_value(row, "checkpoints"), "checkpoint")
        outbox = _require_singleton(_record_value(row, "outboxes"), "outbox")
        _optional_singleton(_record_value(row, "dispositions"), "disposition")
        result = property_map(_record_value(row, "result"), "result")
        image = property_map(_record_value(row, "image"), "image")
        unit = authorization.unit
        fence = authorization.fence
        mutation = authorization.mutation
        rollback_image = authorization.image
        _require_equal(result, "rollback_image_id", rollback_image.rollback_image_id)
        _require_equal(result, "rollback_image_digest", rollback_image.image_digest)
        _require_equal(result, "checkpoint_id", _required_string(checkpoint, "checkpoint_id"))
        _require_equal(result, "outbox_event_id", _required_string(outbox, "event_id"))
        _require_equal(
            result, "new_source_record_pk", _required_string(result, "new_source_record_pk")
        )
        _require_equal(image, "payload_digest", mutation.payload_digest)
        _require_equal(image, "evidence_digest", mutation.evidence_digest)
        _require_equal(result, "evidence_digest", mutation.evidence_digest)
        _require_equal(result, "request_digest", _required_string(result, "request_digest"))
        _require_scope(
            checkpoint,
            unit,
            fence.owner_id,
            "fence_token",
            fence.token,
            mutation.evidence_digest,
        )
        _require_scope(
            outbox,
            unit,
            fence.owner_id,
            "delivery_token",
            fence.token,
            mutation.evidence_digest,
        )
        _require_equal(outbox, "mutation_id", mutation.mutation_id)
        _require_equal(image, "rollback_image_id", rollback_image.rollback_image_id)
        expected_checkpoint_digest = object_digest(
            b"crm-deal-identity-repair-checkpoint-v1" + bytes([0]),
            {"result_digest": _required_string(result, "result_digest")},
        )
        _require_equal(checkpoint, "checkpoint_digest", expected_checkpoint_digest)
        expected_outbox_digest = outbox_event_digest(
            {
                "run_id": unit.run_id,
                "unit_id": unit.unit_id,
                "mutation_id": mutation.mutation_id,
                "result_digest": _required_string(result, "result_digest"),
            }
        )
        _require_equal(outbox, "payload_digest", expected_outbox_digest)

    def _assert_decoded_bundle_bindings(
        self,
        row: Record,
        authorization: RepairRollbackAuthorization,
        bundle: RollbackImageBundle,
    ) -> None:
        """Bind result-only #309 fields to the now validated canonical payload."""
        result = property_map(_record_value(row, "result"), "result")
        body = bundle.payload.get("payload")
        if not isinstance(body, dict):
            raise RepairRollbackDriftError("rollback image body is malformed")
        request = body.get("request")
        if not isinstance(request, dict):
            raise RepairRollbackDriftError("rollback image request is malformed")
        authority_context = body.get("authority_context")
        if not isinstance(authority_context, dict):
            raise RepairRollbackDriftError("rollback image authority context is malformed")
        authority_digest = _required_string(authority_context, "authority_digest")
        if set(authority_context) != {
            "current_owner_ids",
            "authority_digest",
            "external_authority_digest",
        }:
            raise RepairRollbackDriftError("rollback image authority context shape differs")
        owner_ids = authority_context.get("current_owner_ids")
        if not isinstance(owner_ids, list):
            raise RepairRollbackDriftError("rollback image authority owners are malformed")
        normalized_owner_ids = [
            owner_id for owner_id in owner_ids if isinstance(owner_id, str) and owner_id
        ]
        if len(normalized_owner_ids) != len(owner_ids) or normalized_owner_ids != sorted(
            set(normalized_owner_ids)
        ):
            raise RepairRollbackDriftError("rollback image authority owners are malformed")
        _required_string(authority_context, "external_authority_digest")
        _require_equal(result, "evidence_digest", authority_digest)
        _require_equal(
            property_map(_record_value(row, "image"), "image"),
            "evidence_digest",
            authority_digest,
        )
        if mutation_request_digest(request) != _required_string(result, "request_digest"):
            raise RepairRollbackDriftError("rollback mutation request digest differs")
        expected_request: dict[str, JsonValue] = {
            "owner_id": authorization.fence.owner_id,
            "fence_id": authorization.fence.fence_id,
            "fence_token": authorization.fence.token,
            "unit_fingerprint": authorization.unit.inventory_fingerprint,
            "inventory_key": authorization.unit.inventory_key or "",
            "inventory_fingerprint": authorization.unit.inventory_graph_fingerprint or "",
            "inventory_binding_digest": authorization.unit.inventory_binding_digest or "",
            "stored_payload_fingerprint": (
                authorization.unit.inventory_stored_payload_fingerprint or ""
            ),
            "source_instance_id": bundle.source_instance_id,
            "control_instance_id": bundle.control_instance_id,
            "mutation_contract_version": "crm_deal_identity_repair_mutation_v1",
        }
        for key, expected_value in expected_request.items():
            _require_equal(request, key, expected_value)
        _require_equal(result, "new_source_record_pk", bundle.replacement_source_record_pk)
        _require_equal(
            result, "repaired_state_digest", authorization.image.expected_repaired_digest
        )
        if repaired_state_digest(bundle.expected_repaired_state) != _required_string(
            result, "repaired_state_digest"
        ):
            raise RepairRollbackDriftError("rollback mutation repaired-state digest differs")
        desired_state = body.get("desired_state")
        if not isinstance(desired_state, dict):
            raise RepairRollbackDriftError("rollback image desired state is malformed")
        expected_result_digest = mutation_result_digest(
            {
                "request_digest": _required_string(result, "request_digest"),
                "authority_digest": authority_digest,
                "rollback_image_digest": authorization.image.image_digest,
                "expected_repaired_digest": authorization.image.expected_repaired_digest,
                "desired_state": desired_state,
            }
        )
        if _required_string(result, "result_digest") != expected_result_digest:
            raise RepairRollbackDriftError("rollback mutation result digest differs")
        if authorization.unit.source_record_pk != bundle.source_record_pk:
            raise RepairRollbackDriftError("rollback unit/image original source differs")

    def _assert_persisted_authorization(
        self,
        row: Record,
        authorization: RepairRollbackAuthorization,
        *,
        terminal: bool,
    ) -> None:
        """Require the independently persisted transition, never a caller-derived approval."""
        values = property_map(_record_value(row, "authorization"), "authorization")
        expected: dict[str, JsonValue] = {
            "run_id": authorization.unit.run_id,
            "unit_id": authorization.unit.unit_id,
            "authorization_transition_id": authorization.authorization_transition_id,
            "authorization_reference": authorization.authorization_reference,
            "authorization_token": authorization.authorization_token,
            "predecessor_transition_id": authorization.predecessor_transition_id,
            "authorization_policy": authorization.authorization_policy,
            "generation": authorization.unit.generation,
            "sequence": authorization.unit.sequence,
            "attempt": authorization.unit.attempt,
            "boundary_digest": authorization.unit.boundary_digest,
            "fence_id": authorization.fence.fence_id,
            "owner_id": authorization.fence.owner_id,
            "fence_token": authorization.fence.token,
            "mutation_id": authorization.mutation.mutation_id,
            "rollback_image_id": authorization.image.rollback_image_id,
            "image_digest": authorization.image.image_digest,
        }
        for key, expected_value in expected.items():
            _require_equal(values, key, expected_value)
        if terminal:
            image = property_map(_record_value(row, "image"), "image")
            result_digest = _required_string(image, "rollback_result_digest")
            disposition = _require_singleton(_record_value(row, "dispositions"), "disposition")
            disposition_id = _required_string(disposition, "disposition_id")
            self._assert_consumed_authorization(
                values, authorization, disposition_id, result_digest
            )
        elif authorization.fence.state != "claimed":
            raise RepairRollbackAuthorityError("rollback fence is no longer claimed")
        elif values.get("state") != "approved" or values.get("consumable") is not True:
            raise RepairRollbackAuthorityError("rollback authorization is unavailable")

    @staticmethod
    def _assert_consumed_authorization(
        values: dict[str, JsonValue],
        authorization: RepairRollbackAuthorization,
        disposition_id: str,
        result_digest: str,
    ) -> None:
        if values.get("state") != "consumed" or values.get("consumable") is not False:
            raise RepairRollbackDriftError("rollback authorization consumption state differs")
        _require_equal(values, "consumed_disposition_id", disposition_id)
        request_digest = rollback_request_digest(
            {
                "contract_version": "crm_deal_identity_repair_rollback_v1",
                "authorization": authorization.to_dict(),
            }
        )
        _require_equal(values, "consumed_request_digest", request_digest)
        _require_equal(values, "consumed_result_digest", result_digest)

    def _assert_terminal_disposition(
        self,
        values: dict[str, JsonValue],
        command: RepairRollbackCommand,
        authorization: RepairRollbackAuthorization,
        outcome: str,
    ) -> None:
        expected: dict[str, str | int] = {
            "run_id": authorization.unit.run_id,
            "unit_id": authorization.unit.unit_id,
            "disposition_id": command.disposition_id,
            "generation": authorization.unit.generation,
            "sequence": authorization.unit.sequence,
            "attempt": authorization.unit.attempt,
            "owner_id": authorization.fence.owner_id,
            "control_token": authorization.fence.token,
            "boundary_digest": authorization.unit.boundary_digest,
            "subject_fingerprint": authorization.image.image_digest,
            "evidence_digest": authorization.image.evidence_digest,
            "payload_digest": command.request_digest,
            "outcome": outcome,
            "rollback_request_digest": command.request_digest,
            "authorization_reference": authorization.authorization_reference,
            "authorization_token": authorization.authorization_token,
            "predecessor_transition_id": authorization.predecessor_transition_id,
            "authorization_policy": authorization.authorization_policy,
            "authorization_transition_id": authorization.authorization_transition_id,
            "rollback_image_id": authorization.image.rollback_image_id,
        }
        for key, value in expected.items():
            _require_equal(values, key, value)


def _record_value(row: Record, key: str) -> object:
    try:
        return row[key]
    except KeyError as exc:
        raise RepairRollbackRecordError("rollback record is missing: " + key) from exc


def _result_request_digest(row: Record) -> str:
    result = property_map(_record_value(row, "result"), "result")
    return _required_string(result, "request_digest")


def _required_string(values: dict[str, JsonValue], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RepairRollbackDriftError("rollback terminal property is malformed: " + key)
    return value


def _require_equal(values: dict[str, JsonValue], key: str, expected: JsonValue) -> None:
    if values.get(key) != expected:
        raise RepairRollbackDriftError("rollback immutable property differs: " + key)


def _require_scope(
    values: dict[str, JsonValue],
    unit: object,
    owner_id: str,
    token_key: str,
    token: str,
    evidence_digest: str,
) -> None:
    from src.crm_deal_identity_repair.execution_models import RepairUnit

    if not isinstance(unit, RepairUnit):
        raise RepairRollbackDriftError("rollback unit scope is malformed")
    expected: dict[str, JsonValue] = {
        "run_id": unit.run_id,
        "unit_id": unit.unit_id,
        "generation": unit.generation,
        "sequence": unit.sequence,
        "attempt": unit.attempt,
        "owner_id": owner_id,
        "boundary_digest": unit.boundary_digest,
        "evidence_digest": evidence_digest,
    }
    for key, value in expected.items():
        _require_equal(values, key, value)
    if _required_string(values, token_key) != token:
        raise RepairRollbackDriftError("rollback immutable token differs")


def _optional_singleton(value: object, name: str) -> dict[str, JsonValue] | None:
    if not isinstance(value, list):
        raise RepairRollbackDriftError("rollback " + name + " cardinality is malformed")
    rows = [property_map(item, name) for item in value if item is not None]
    if len(rows) > 1:
        raise RepairRollbackDriftError("rollback " + name + " cardinality differs")
    return rows[0] if rows else None


def _require_singleton(value: object, name: str) -> dict[str, JsonValue]:
    row = _optional_singleton(value, name)
    if row is None:
        raise RepairRollbackDriftError("rollback " + name + " is missing")
    return row
