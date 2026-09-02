"""One repository-managed CAS transaction for exact #309 CRM repair rollback."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import cast

from neo4j import ManagedTransaction

from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackCommand,
    RepairRollbackResult,
    RepairRollbackStatus,
    RollbackFailureStage,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_boundary_evidence import neo4j_json_value
from src.graph.crm_deal_identity_repair_ledger_records import (
    StoredQualification,
    stored_qualification_from_values,
)
from src.graph.crm_deal_identity_repair_rollback_image import (
    RollbackImageBundle,
    decode_rollback_image,
)
from src.graph.crm_deal_identity_repair_rollback_ledger import (
    RepairRollbackAuthorityError,
    RepairRollbackDriftError,
    RollbackLedgerMixin,
)
from src.graph.crm_deal_identity_repair_rollback_restoration import restore_rows
from src.graph.crm_deal_identity_repair_rollback_state import (
    compare_current_state,
    desired_post_rollback_state,
    normalize_post_rollback_state,
    postcondition_history_matches,
)
from src.graph.queries.crm_deal_identity_repair_rollback import (
    LOCK_AND_ASSERT_ROLLBACK_DOMAIN_GUARD,
    LOCK_AND_READ_ROLLBACK_BUNDLE,
    MAKE_MUTATION_EVIDENCE_HISTORICAL,
    READ_ROLLBACK_CURRENT_STATE,
    READ_ROLLBACK_POSTCONDITION,
    RESTORE_ORIGINAL_SOURCE,
    RESTORE_PREEXISTING_RELATIONSHIPS,
)
from src.models import JsonValue


class CrmDealIdentityRepairRollbackRepository(RollbackLedgerMixin):
    """Restore one valid image atomically or record one deterministic reviewed compensation."""

    def __init__(
        self,
        client: Neo4jClient,
        *,
        failpoint: Callable[[RollbackFailureStage], None] | None = None,
    ) -> None:
        self._client = client
        self._failpoint = failpoint

    def commit_atomic_rollback(self, command: RepairRollbackCommand) -> RepairRollbackResult:
        """Use one managed write callback for replay, guards, comparison, and mutation."""
        return self._client.execute_write(lambda tx: self._commit(tx, command))

    def get_rollback_status(self, command: RepairRollbackCommand) -> RepairRollbackStatus:
        """Return only an immutable terminal/available status after exact bundle validation."""
        return self._client.execute_read(lambda tx: self._read_status(tx, command))

    def _commit(
        self, tx: ManagedTransaction, command: RepairRollbackCommand
    ) -> RepairRollbackResult:
        terminal = self._terminal(tx, command)
        if terminal is not None:
            return terminal
        params = self._params(command)
        row = tx.run(LOCK_AND_READ_ROLLBACK_BUNDLE, **params).single()
        if row is None:
            # A concurrent first execution can consume the transition between
            # the initial terminal read and this guarded lock attempt.  Re-read
            # the complete immutable terminal bundle before rejecting it.
            terminal = self._terminal(tx, command)
            if terminal is not None:
                return terminal
            raise RepairRollbackAuthorityError("rollback run/unit/fence authority rejected")
        # The lock query can have matched an available transition before it
        # waited behind another writer.  Re-read the complete terminal bundle
        # while still in this managed write transaction before trusting the
        # returned pre-lock projection.
        terminal = self._terminal(tx, command)
        if terminal is not None:
            return terminal
        authorization, image_payload = self._read_immutable_bundle(row, command)
        self._assert_persisted_authorization(row, authorization, terminal=False)
        result = row["result"]
        if not isinstance(result, dict):
            raise RepairRollbackDriftError("rollback result record is malformed")
        request_digest = result.get("request_digest")
        if not isinstance(request_digest, str) or not request_digest:
            raise RepairRollbackDriftError("rollback result request digest is malformed")
        bundle = decode_rollback_image(
            authorization.image, authorization.mutation, image_payload, request_digest
        )
        self._assert_decoded_bundle_bindings(row, authorization, bundle)
        self._domain_guard(tx, command, bundle)
        self._fail("after_guard")
        current = self._current_state(tx, bundle)
        self._fail("after_lock")
        drift = compare_current_state(bundle, current)
        self._fail("after_compare")
        if drift is not None:
            return self._persist_terminal(tx, command, "reviewed_compensation_required", drift)
        self._restore(tx, bundle, command)
        self._fail("after_restore")
        self._assert_restored(tx, bundle, command.authorization.image.rollback_image_id)
        self._fail("after_postcondition")
        result = self._persist_terminal(tx, command, "restored", None)
        self._fail("after_ledger")
        return result

    def _current_state(
        self, tx: ManagedTransaction, bundle: RollbackImageBundle
    ) -> dict[str, JsonValue]:
        row = tx.run(
            READ_ROLLBACK_CURRENT_STATE,
            mutation_id=bundle.mutation_id,
            source_record_pk=bundle.source_record_pk,
            retired_source_record_pks=list(bundle.retired_source_record_pks),
        ).single()
        if row is None:
            return {"nodes": [], "relationships": []}
        return {
            "nodes": _json_rows(row["nodes"]),
            "relationships": _json_rows(row["relationships"]),
        }

    def _domain_guard(
        self,
        tx: ManagedTransaction,
        command: RepairRollbackCommand,
        bundle: RollbackImageBundle,
    ) -> None:
        row = tx.run(
            LOCK_AND_ASSERT_ROLLBACK_DOMAIN_GUARD,
            run_id=command.authorization.unit.run_id,
            unit_id=command.authorization.unit.unit_id,
            mutation_id=bundle.mutation_id,
            boundary_digest=command.authorization.unit.boundary_digest,
            source_instance_id=bundle.source_instance_id,
            control_instance_id=bundle.control_instance_id,
            original_source_record_pk=bundle.source_record_pk,
            quoted_original_source_record_pk=json.dumps(bundle.source_record_pk),
            source_record_id=bundle.source_record_id,
            replacement_source_record_pk=bundle.replacement_source_record_pk,
            authorization_reference=command.authorization.authorization_reference,
            authorization_policy=command.authorization.authorization_policy,
        ).single()
        if row is None:
            raise RepairRollbackAuthorityError("rollback domain guard rejected")
        stored = _stored_qualification_from_domain_guard(row)
        if stored.run.run_id != command.authorization.unit.run_id:
            raise RepairRollbackDriftError("rollback qualified run identity differs")
        if bundle.source_record_pk not in stored.source_record_pks:
            raise RepairRollbackAuthorityError("rollback source is outside qualified boundary")
        if command.authorization.unit.source_record_pk != bundle.source_record_pk:
            raise RepairRollbackDriftError("rollback unit/image original source differs")
        if bundle.replacement_source_record_pk == bundle.source_record_pk:
            raise RepairRollbackDriftError("rollback replacement source equals original")
        if (
            stored.manifest.rollback_authority_reference
            != command.authorization.authorization_reference
            or stored.manifest.rollback_authority_policy
            != command.authorization.authorization_policy
        ):
            raise RepairRollbackAuthorityError("rollback manifest authorization changed")

    def _restore(
        self, tx: ManagedTransaction, bundle: RollbackImageBundle, command: RepairRollbackCommand
    ) -> None:
        sources = [
            {"source_record_pk": bundle.source_record_pk, "properties": bundle.source_properties},
            *[
                {"source_record_pk": source_pk, "properties": properties}
                for source_pk, properties in bundle.descendant_properties
            ],
        ]
        source = tx.run(
            RESTORE_ORIGINAL_SOURCE,
            sources=sources,
        ).single()
        if source is None or source["restored_count"] != len(sources):
            raise RepairRollbackDriftError("rollback source restoration cardinality differs")
        rows = restore_rows(bundle.relationship_rows)
        restored = tx.run(RESTORE_PREEXISTING_RELATIONSHIPS, relationships=rows).single()
        assignment_count = sum(_assignment_count(item) for item in rows)
        if restored is None or restored["restored_count"] != assignment_count:
            raise RepairRollbackDriftError("rollback relationship restoration cardinality differs")
        historical = tx.run(
            MAKE_MUTATION_EVIDENCE_HISTORICAL,
            replacement_source_record_pk=bundle.replacement_source_record_pk,
            mutation_id=bundle.mutation_id,
            rollback_image_id=command.authorization.image.rollback_image_id,
        ).single()
        if historical is None:
            raise RepairRollbackDriftError("rollback replacement evidence is missing")

    def _assert_restored(
        self, tx: ManagedTransaction, bundle: RollbackImageBundle, rollback_image_id: str
    ) -> None:
        row = tx.run(
            READ_ROLLBACK_POSTCONDITION,
            source_record_pk=bundle.source_record_pk,
            retired_source_record_pks=list(bundle.retired_source_record_pks),
            replacement_source_record_pk=bundle.replacement_source_record_pk,
            mutation_id=bundle.mutation_id,
            rollback_image_id=rollback_image_id,
        ).single()
        if row is None:
            raise RepairRollbackDriftError("rollback postcondition is missing")
        observed: dict[str, JsonValue] = {
            "nodes": _json_rows(row["sources"]),
            "relationships": _json_rows(row["relationships"]),
        }
        if normalize_post_rollback_state(observed) != desired_post_rollback_state(
            bundle, rollback_image_id
        ):
            raise RepairRollbackDriftError("rollback postcondition differs")
        if not postcondition_history_matches(
            bundle,
            rollback_image_id,
            _json_value(row["replacement"], "replacement"),
            _json_value(row["mutation_nodes"], "mutation_nodes"),
            _json_value(row["mutation_relationships"], "mutation_relationships"),
        ):
            raise RepairRollbackDriftError("rollback historical evidence postcondition differs")

    def _fail(self, stage: RollbackFailureStage) -> None:
        if self._failpoint is not None:
            self._failpoint(stage)


def _assignment_count(value: dict[str, JsonValue]) -> int:
    assignments = value.get("assignments")
    if not isinstance(assignments, list):
        raise RepairRollbackDriftError("rollback restore assignments are malformed")
    return len(assignments)


def _json_rows(value: object) -> list[JsonValue]:
    if not isinstance(value, list):
        raise RepairRollbackDriftError("rollback current-state collection is malformed")
    rows: list[JsonValue] = []
    for item in value:
        converted = _json_value(item, "collection row")
        if not isinstance(converted, dict):
            raise RepairRollbackDriftError("rollback current-state collection row is malformed")
        rows.append(converted)
    return rows


def _json_value(value: object, name: str) -> JsonValue:
    """Convert driver-native graph values through the canonical #309 evidence codec."""
    try:
        return neo4j_json_value(value)
    except RuntimeError as exc:
        raise RepairRollbackDriftError("rollback " + name + " is malformed") from exc


def _stored_qualification_from_domain_guard(row: object) -> StoredQualification:
    """Strictly decode the canonical #300 manifest returned by the locked guard."""
    if not isinstance(row, Mapping):
        raise RepairRollbackDriftError("rollback domain guard record is malformed")
    run = row.get("run")
    boundaries = row.get("boundaries")
    qualification_link_count = row.get("qualification_link_count")
    if (
        not isinstance(run, Mapping)
        or not isinstance(boundaries, list)
        or not all(isinstance(boundary, Mapping) for boundary in boundaries)
        or isinstance(qualification_link_count, bool)
        or not isinstance(qualification_link_count, int)
    ):
        raise RepairRollbackDriftError("rollback qualified manifest is malformed")
    repair_id = run.get("repair_id")
    if not isinstance(repair_id, str) or not repair_id:
        raise RepairRollbackDriftError("rollback qualified repair identity is malformed")
    run_values = _json_mapping(run, "run")
    boundary_values: list[JsonValue] = [
        cast(JsonValue, _json_mapping(boundary, "boundary"))
        for boundary in cast(list[Mapping[object, object]], boundaries)
    ]
    values: dict[str, JsonValue] = {
        **run_values,
        "qualification_link_count": qualification_link_count,
        "boundaries": boundary_values,
    }
    try:
        return stored_qualification_from_values(repair_id, values)
    except RuntimeError as exc:
        raise RepairRollbackDriftError("rollback qualified manifest is invalid") from exc


def _json_mapping(value: Mapping[object, object], name: str) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RepairRollbackDriftError("rollback qualified " + name + " keys are malformed")
        result[key] = cast(JsonValue, item)
    return result
