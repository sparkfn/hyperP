"""Read-only run-equation and frozen negative-control aggregation."""

from __future__ import annotations

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.inventory import rebuild_inventory_payload
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.qualification_inventory import inventory_payload_fingerprints
from src.crm_deal_identity_repair.verification_models import (
    RepairRunEquationCommand,
    RepairRunEquationResult,
)
from src.graph.crm_deal_identity_repair_ledger_records import canonical_json_text
from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
from src.graph.crm_deal_identity_repair_verification_support import (
    json_list,
    json_object,
    json_scalar,
    nonnegative_row_count,
)
from src.graph.queries import crm_deal_identity_repair_verification as queries
from src.models import JsonValue


def read_run_equation(
    tx: ManagedTransaction, command: RepairRunEquationCommand
) -> RepairRunEquationResult:
    """Return a read-only accounting result bound to authenticated frozen inventory."""
    counts = tx.run(
        queries.READ_RUN_VERIFICATION_COUNTS,
        repair_id=command.repair_id,
        run_id=command.run_id,
        boundary_digest=command.boundary_digest,
        inventory_digest=command.inventory_digest_expected,
        source_instance_id=command.source_instance_id,
        control_instance_id=command.control_instance_id,
        source_record_pks_json=canonical_source_record_pks_json(command.inventory),
    ).single()
    if counts is None:
        raise RepairVerificationDriftError("run equation immutable boundary differs")
    frozen_pks = frozen_executable_closure_pks(command.inventory)
    graph = tx.run(
        queries.READ_RUN_GRAPH_TOTALS, frozen_source_record_pks=list(frozen_pks)
    ).single()
    if graph is None:
        raise RepairVerificationDriftError("run equation graph totals are missing")
    negatives = tuple(item for item in command.inventory if item.partition == "negative_control")
    snapshot = list(
        tx.run(
            queries.READ_NEGATIVE_CONTROL_FULL_STATE,
            items=negative_control_query_items(negatives),
        )
    )
    classifications = classify_negative_controls(negatives, snapshot)
    payload: dict[str, JsonValue] = {key: json_scalar(counts[key]) for key in counts.keys()}
    payload.update(
        {
            "negative_controls": len(negatives),
            "missing": classifications.count("missing"),
            "stamped": classifications.count("stamped"),
        }
    )
    evidence = object_digest(b"crm-deal-identity-repair-run-equation-evidence-v1\x00", payload)
    return RepairRunEquationResult(
        qualified_inventory_rows=len(command.inventory),
        executable_inventory_rows=len(command.inventory) - len(negatives),
        negative_control_rows=len(negatives),
        applied_units=required_count(counts, "applied_units"),
        review_required_units=required_count(counts, "review_required_units"),
        incomplete_units=required_count(counts, "incomplete_units"),
        verified_units=required_count(counts, "verified_units"),
        drifted_units=required_count(counts, "drifted_units"),
        failed_units=required_count(counts, "failed_units"),
        committed_attempts=required_count(counts, "committed_attempts"),
        replay_no_op_attempts=required_count(counts, "replay_no_op_attempts"),
        active_links=required_count(graph, "active_links"),
        unsupported_multi_links=required_count(graph, "unsupported_multi_links"),
        active_deal_origin_phone_projections=required_count(graph, "phones"),
        active_deal_origin_email_projections=required_count(graph, "emails"),
        active_deal_origin_g_us_projections=required_count(graph, "groups"),
        reconciled_secondaries=required_count(counts, "reconciled_secondaries"),
        review_required_secondaries=required_count(counts, "review_required_secondaries"),
        failed_secondaries=required_count(counts, "failed_secondaries"),
        pending_secondaries=required_count(counts, "pending_secondaries"),
        expected_secondary_count=required_count(counts, "expected_secondary_count"),
        observed_secondary_count=required_count(counts, "observed_secondary_count"),
        unexplained_secondary_remainder=_unexplained_secondaries(counts),
        unchanged_negative_controls=classifications.count("unchanged"),
        drifted_negative_controls=classifications.count("drifted"),
        missing_negative_controls=classifications.count("missing"),
        stamped_negative_controls=classifications.count("stamped"),
        evidence_digest=evidence,
    )


def _unexplained_secondaries(row: Record) -> int:
    expected = required_count(row, "expected_secondary_count")
    observed = required_count(row, "observed_secondary_count")
    terminal = sum(
        required_count(row, key)
        for key in (
            "reconciled_secondaries",
            "review_required_secondaries",
            "failed_secondaries",
            "pending_secondaries",
        )
    )
    return abs(expected - observed) + abs(observed - terminal)


def frozen_executable_closure_pks(
    inventory: tuple[RepairInventoryItem, ...],
) -> tuple[str, ...]:
    """Return only immutable executable roots and authenticated descendant identities."""
    values: set[str] = set()
    for item in inventory:
        if item.partition == "negative_control":
            continue
        values.add(item.source_record_pk)
        descendants = item.payload.get("descendants")
        if not isinstance(descendants, list):
            raise RepairVerificationDriftError("frozen descendant closure is malformed")
        for descendant in descendants:
            if not isinstance(descendant, dict):
                raise RepairVerificationDriftError("frozen descendant closure is malformed")
            source_record_pk = descendant.get("source_record_pk")
            if not isinstance(source_record_pk, str) or not source_record_pk:
                raise RepairVerificationDriftError("frozen descendant identity is malformed")
            values.add(source_record_pk)
    return tuple(sorted(values))


def negative_control_query_items(
    items: tuple[RepairInventoryItem, ...],
) -> list[dict[str, JsonValue]]:
    """Bind state reads to frozen root/descendant identity closures only."""
    values: list[dict[str, JsonValue]] = []
    for item in items:
        descendants = item.payload.get("descendants")
        if not isinstance(descendants, list):
            raise RepairVerificationDriftError("negative-control descendant closure is malformed")
        closure = {item.source_record_pk}
        for descendant in descendants:
            if not isinstance(descendant, dict):
                raise RepairVerificationDriftError(
                    "negative-control descendant closure is malformed"
                )
            source_record_pk = descendant.get("source_record_pk")
            if not isinstance(source_record_pk, str) or not source_record_pk:
                raise RepairVerificationDriftError(
                    "negative-control descendant identity is malformed"
                )
            closure.add(source_record_pk)
        closure_values: list[JsonValue] = []
        for source_record_pk in sorted(closure):
            closure_values.append(source_record_pk)
        values.append(
            {
                "source_record_pk": item.source_record_pk,
                "closure_source_record_pks": closure_values,
            }
        )
    return values


def canonical_source_record_pks_json(
    inventory: tuple[RepairInventoryItem, ...],
) -> str:
    """Use the exact #300 canonical object shape stored on qualified runs."""
    source_record_pks: list[JsonValue] = []
    for source_record_pk in sorted(item.source_record_pk for item in inventory):
        source_record_pks.append(source_record_pk)
    return canonical_json_text(
        {"source_record_pks": source_record_pks},
        "run equation source record identities",
    )


def required_count(row: Record, key: str) -> int:
    value: object = row[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RepairVerificationDriftError("run equation count is malformed")
    return value


def classify_negative_controls(
    items: tuple[RepairInventoryItem, ...], rows: list[Record]
) -> tuple[str, ...]:
    expected = {item.source_record_pk: item for item in items}
    observed: dict[str, Record] = {}
    for row in rows:
        source_record_pk = row["source_record_pk"]
        if not isinstance(source_record_pk, str) or source_record_pk not in expected:
            raise RepairVerificationDriftError("negative-control snapshot identity differs")
        if source_record_pk in observed:
            raise RepairVerificationDriftError("negative-control snapshot is duplicated")
        observed[source_record_pk] = row
    if set(observed) != set(expected):
        raise RepairVerificationDriftError("negative-control snapshot is incomplete")
    values: list[str] = []
    for source_record_pk, item in sorted(expected.items()):
        row = observed[source_record_pk]
        source = row["source_properties"]
        if source is None:
            values.append("missing")
            continue
        if (
            nonnegative_row_count(row, "graph_stamp_count") > 0
            or nonnegative_row_count(row, "ledger_stamp_count") > 0
        ):
            values.append("stamped")
            continue
        payload = rebuild_inventory_payload(
            json_object(source),
            json_list(row, "linked_people"),
            json_list(row, "projections"),
            json_list(row, "logical_versions"),
            json_list(row, "descendants"),
            json_list(row, "decisions_and_reviews"),
            json_list(row, "owner_impacts"),
        )
        graph_fingerprint, stored_payload_fingerprint = inventory_payload_fingerprints(payload)
        linked_people = payload["linked_people"]
        projections = payload["projections"]
        complete_relationship_snapshot = (
            isinstance(linked_people, list)
            and isinstance(projections, list)
            and len(linked_people) == nonnegative_row_count(row, "link_row_count")
            and len(projections) == nonnegative_row_count(row, "projection_row_count")
        )
        if (
            nonnegative_row_count(row, "source_system_matches") != 1
            or not complete_relationship_snapshot
            or graph_fingerprint != item.graph_fingerprint
            or stored_payload_fingerprint != item.stored_payload_fingerprint
            or payload != item.payload
        ):
            values.append("drifted")
        else:
            values.append("unchanged")
    return tuple(values)
