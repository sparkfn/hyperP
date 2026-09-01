"""Read-only acknowledged replay validation for CRM repair verification."""

from __future__ import annotations

from collections.abc import Mapping

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import disposition_digest, verification_result_digest
from src.crm_deal_identity_repair.execution_models import RepairSecondaryDisposition
from src.crm_deal_identity_repair.verification_models import (
    RepairAtomicVerificationResult,
    RepairVerificationCommand,
)
from src.golden_profile import derive_golden_profile_from_active_authority
from src.graph.crm_deal_identity_repair_mutation_records import outbox_event_from_properties
from src.graph.crm_deal_identity_repair_verification_derived import (
    PersonDerivedState,
    affected_person_ids,
    build_context_details,
    derive_state_digest,
    expected_subject_keys,
    read_person_states,
    verify_replayed_revision,
)
from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
from src.graph.crm_deal_identity_repair_verification_pair import read_pair_snapshot
from src.graph.crm_deal_identity_repair_verification_records import VerificationBundle
from src.graph.crm_deal_identity_repair_verification_support import (
    disposition_from_properties,
    list_mappings,
    mapping,
    postcondition_closure_source_record_pks,
    primary_matches,
    required_int,
    required_str,
    retired_source_record_pks,
    verification_from_properties,
)
from src.graph.queries import crm_deal_identity_repair_verification as queries
from src.models import JsonValue


def replay_acknowledged_verification(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    bundle: VerificationBundle,
) -> RepairAtomicVerificationResult:
    """Re-read every derived boundary and ledger binding without performing writes."""
    row = tx.run(
        queries.READ_ACKNOWLEDGED_VERIFICATION,
        run_id=command.unit.run_id,
        unit_id=command.unit.unit_id,
        event_id=command.outbox_event_id,
        request_digest=command.request_digest,
        verification_id=command.verification_id,
    ).single()
    if row is None:
        raise RepairVerificationDriftError("acknowledged verification request differs")
    verification_values = mapping(row, "verification")
    disposition_values = list_mappings(row, "dispositions")
    verification = verification_from_properties(verification_values)
    dispositions = tuple(disposition_from_properties(value) for value in disposition_values)
    outbox_values = mapping(row, "outbox")
    _validate_acknowledged(
        command,
        bundle,
        verification_values,
        outbox_values,
        disposition_values,
        dispositions,
    )
    person_ids = affected_person_ids(
        tx, (*retired_source_record_pks(command), bundle.replacement_pk)
    )
    primary = _read_primary(tx, command, bundle)
    states = read_person_states(tx, person_ids)
    _validate_crm_deal_counts(tx, states)
    _validate_profiles(tx, states)
    build_context_details(tx, command)
    verify_replayed_revision(tx, command, bundle.replacement_pk, bundle.result.outcome)
    _validate_subject_set(command, states, bundle.replacement_pk, disposition_values)
    state = derive_state_digest(primary, dispositions, states, read_pair_snapshot(tx, command))
    if state != verification.payload_digest:
        raise RepairVerificationDriftError("acknowledged derived state differs")
    return RepairAtomicVerificationResult(
        "replayed",
        verification,
        dispositions,
        outbox_event_from_properties(outbox_values),
        None,
        state,
    )


def _read_primary(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    bundle: VerificationBundle,
) -> Record:
    row = tx.run(
        queries.READ_PRIMARY_POSTCONDITIONS,
        new_source_record_pk=bundle.replacement_pk,
        mutation_id=command.mutation_id,
        retired_source_record_pks=list(retired_source_record_pks(command)),
        closure_source_record_pks=list(
            postcondition_closure_source_record_pks(command, bundle.replacement_pk)
        ),
    ).single()
    if row is None or not primary_matches(row, bundle.result.outcome):
        raise RepairVerificationDriftError("acknowledged primary state differs")
    return row


def _validate_profiles(tx: ManagedTransaction, states: tuple[PersonDerivedState, ...]) -> None:
    for state in states:
        profile = derive_golden_profile_from_active_authority(tx, state.person_id)
        if profile is None or profile.changed:
            raise RepairVerificationDriftError("acknowledged golden profile differs")


def _validate_crm_deal_counts(
    tx: ManagedTransaction,
    states: tuple[PersonDerivedState, ...],
) -> None:
    rows = tuple(
        tx.run(
            queries.READ_EXPECTED_AFFECTED_CRM_DEAL_COUNTS,
            person_ids=[state.person_id for state in states],
        )
    )
    expected = tuple((state.person_id, state.crm_deal_count) for state in states)
    observed = tuple(
        (required_str(row, "person_id"), required_int_row(row, "expected_crm_deal_count"))
        for row in rows
    )
    if observed != expected:
        raise RepairVerificationDriftError("acknowledged CRM deal count differs")


def _validate_acknowledged(
    command: RepairVerificationCommand,
    bundle: VerificationBundle,
    verification_values: Mapping[str, JsonValue],
    outbox_values: Mapping[str, JsonValue],
    disposition_values: list[Mapping[str, JsonValue]],
    dispositions: tuple[RepairSecondaryDisposition, ...],
) -> None:
    expected = {
        "run_id": command.unit.run_id,
        "unit_id": command.unit.unit_id,
        "verification_id": command.verification_id,
        "generation": command.unit.generation,
        "sequence": command.unit.sequence,
        "attempt": command.unit.attempt,
        "owner_id": command.owner_id,
        "fence_token": command.fence.token,
        "boundary_digest": command.unit.boundary_digest,
        "subject_fingerprint": command.unit.inventory_fingerprint,
        "evidence_digest": bundle.result.evidence_digest,
        "request_digest": command.request_digest,
        "outcome": "verified",
    }
    for key, expected_value in expected.items():
        observed = verification_values.get(key)
        if observed != expected_value:
            raise RepairVerificationDriftError("acknowledged verification scope differs")
    expected_outbox = {
        "run_id": command.unit.run_id,
        "unit_id": command.unit.unit_id,
        "event_id": command.outbox_event_id,
        "generation": command.unit.generation,
        "sequence": command.unit.sequence,
        "attempt": command.unit.attempt,
        "owner_id": command.owner_id,
        "delivery_token": command.fence.token,
        "boundary_digest": command.unit.boundary_digest,
        "mutation_id": command.mutation_id,
        "payload_digest": bundle.outbox.payload_digest,
        "evidence_digest": bundle.outbox.evidence_digest,
        "state": "acknowledged",
        "verification_request_digest": command.request_digest,
    }
    for key, expected_value in expected_outbox.items():
        if outbox_values.get(key) != expected_value:
            raise RepairVerificationDriftError("acknowledged outbox scope differs")
    expected_digest = verification_result_digest(
        {
            "request_digest": command.request_digest,
            "derived_state_digest": required_str(verification_values, "payload_digest"),
        }
    )
    if required_str(verification_values, "verification_digest") != expected_digest:
        raise RepairVerificationDriftError("acknowledged verification digest differs")
    expected_count = required_int(verification_values, "expected_disposition_count")
    if expected_count != len(dispositions) or not dispositions:
        raise RepairVerificationDriftError("acknowledged disposition count differs")
    if len(dispositions) != len({item.disposition_id for item in dispositions}):
        raise RepairVerificationDriftError("acknowledged dispositions are duplicate")
    for raw, disposition in zip(disposition_values, dispositions, strict=True):
        action = raw.get("action")
        kind = raw.get("subject_kind")
        stable_id = raw.get("subject_stable_id")
        if (
            disposition.outcome == "pending"
            or not isinstance(action, str)
            or not isinstance(kind, str)
            or not isinstance(stable_id, str)
        ):
            raise RepairVerificationDriftError("acknowledged disposition is incomplete")
        if disposition.payload_digest != disposition_digest(
            {
                "subject": disposition.subject_fingerprint,
                "action": action,
                "outcome": disposition.outcome,
            }
        ):
            raise RepairVerificationDriftError("acknowledged disposition payload differs")
        if (
            disposition.run_id != command.unit.run_id
            or disposition.unit_id != command.unit.unit_id
            or disposition.generation != command.unit.generation
            or disposition.sequence != command.unit.sequence
            or disposition.attempt != command.unit.attempt
            or disposition.owner_id != command.owner_id
            or disposition.control_token != command.claim_token
            or disposition.boundary_digest != command.unit.boundary_digest
        ):
            raise RepairVerificationDriftError("acknowledged disposition scope differs")


def _validate_subject_set(
    command: RepairVerificationCommand,
    states: tuple[PersonDerivedState, ...],
    replacement_pk: str,
    values: list[Mapping[str, JsonValue]],
) -> None:
    expected = expected_subject_keys(command, states, replacement_pk)
    observed = tuple(
        (required_str(value, "subject_kind"), required_str(value, "subject_stable_id"))
        for value in values
    )
    if len(observed) != len(set(observed)) or tuple(sorted(observed)) != expected:
        raise RepairVerificationDriftError("acknowledged secondary subject set differs")


def required_int_row(row: Record, key: str) -> int:
    value: object = row[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairVerificationDriftError("acknowledged CRM deal count is malformed")
    return value
