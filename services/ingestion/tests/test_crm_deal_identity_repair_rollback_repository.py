"""Behavioural unit coverage for the fenced rollback repository transaction."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar, cast
from uuid import NAMESPACE_URL, uuid5

import pytest
from neo4j import ManagedTransaction, Record
from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.digests import (
    mutation_request_digest,
    mutation_result_digest,
    object_digest,
    outbox_event_digest,
    repaired_state_digest,
    rollback_image_digest,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairExecutionBoundaryManifest,
    RepairFence,
    RepairMutationResult,
    RepairRollbackImage,
    RepairUnit,
)
from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackAuthorization,
    RepairRollbackCommand,
    RepairRollbackDrift,
    RollbackFailureStage,
    build_rollback_result_digest,
    build_rollback_status_digest,
)
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_rollback import (
    CrmDealIdentityRepairRollbackRepository,
    RepairRollbackAuthorityError,
    RepairRollbackDriftError,
    _stored_qualification_from_domain_guard,
)
from src.graph.crm_deal_identity_repair_rollback_image import (
    RollbackImageBundle,
    decode_rollback_image,
)
from src.graph.queries import crm_deal_identity_repair_rollback as queries
from src.models import JsonValue

T = TypeVar("T")
_DIGEST = "sha256:" + "a" * 64


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _Transaction:
    def __init__(self, rows: dict[str, list[dict[str, object] | None]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        self.calls.append((query, params))
        values = self.rows.get(query, [None])
        return _Result(values.pop(0) if values else None)


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self.transaction = transaction
        self.write_calls = 0
        self.read_calls = 0

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        self.write_calls += 1
        return work(cast(ManagedTransaction, self.transaction))

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        self.read_calls += 1
        return work(cast(ManagedTransaction, self.transaction))


def _payload() -> tuple[str, dict[str, JsonValue], str, str]:
    request: dict[str, JsonValue] = {
        "run_id": "run",
        "unit_id": "unit",
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "owner_id": "owner",
        "fence_id": "fence",
        "fence_token": "token",
        "boundary_digest": _DIGEST,
        "unit_fingerprint": _DIGEST,
        "inventory_key": "inventory",
        "inventory_fingerprint": _DIGEST,
        "inventory_binding_digest": _DIGEST,
        "stored_payload_fingerprint": _DIGEST,
        "source_instance_id": "source-instance",
        "control_instance_id": "control-instance",
        "mutation_contract_version": "crm_deal_identity_repair_mutation_v1",
    }
    mutation_id = str(uuid5(NAMESPACE_URL, mutation_request_digest(request)))
    expected: dict[str, JsonValue] = {"nodes": [], "relationships": []}
    created_specs: list[dict[str, JsonValue]] = [
        {
            "object_kind": "SourceRecord",
            "identity": {"source_record_pk": "replacement"},
            "properties": {"repair_mutation_id": mutation_id},
            "preexisting": False,
            "write_mode": "created",
        }
    ]
    payload: dict[str, JsonValue] = {
        "expected_repaired_state": expected,
        "payload": {
            "contract_version": "crm_deal_identity_repair_mutation_v1",
            "request": request,
            "authority_context": {
                "current_owner_ids": ["owner"],
                "authority_digest": _DIGEST,
                "external_authority_digest": _DIGEST,
            },
            "pre_state": {
                "source": {
                    "source_record_pk": "old",
                    "source_record_id": "bitrix-crm-deal-1",
                    "source_instance_id": "source-instance",
                    "lifecycle_status": "active",
                    "is_latest": True,
                },
                "descendants": [],
                "relationships": [],
                "created_identifier_candidates": [],
            },
            "desired_state": {
                "source_record_pk": "replacement",
                "retired_source_record_pks": ["old"],
            },
            "created_object_specifications": created_specs,
            "rollback_operations": [
                {
                    "operation": "delete_created_relationships_by_repair_mutation_id",
                    "repair_mutation_id": mutation_id,
                    "relationship_types": [
                        "LINKED_TO",
                        "ABOUT_LEFT",
                        "ABOUT_RIGHT",
                        "FOR_DECISION",
                        "IDENTIFIED_BY",
                        "HAS_FACT",
                        "FROM_SOURCE",
                        "PREVIOUS_VERSION_OF",
                        "OWNED_BY",
                    ],
                },
                {
                    "operation": "delete_created_nodes_and_identifiers",
                    "source_record_pk": "replacement",
                    "match_decision_id": mutation_id + ":decision",
                    "review_case_id": mutation_id + ":review",
                    "identifier_repair_mutation_id": mutation_id,
                    "identifier_candidates": [],
                    "created_object_specifications": created_specs,
                    "delete_identifier_only_when_preexisting_is_false": True,
                },
                {
                    "operation": "restore_source_and_relationship_properties",
                    "source_record_pk": "old",
                    "relationships": [],
                },
            ],
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return encoded, request, rollback_image_digest(payload), repaired_state_digest(expected)


def _command() -> RepairRollbackCommand:
    _, request, image_digest, expected_digest = _payload()
    mutation_id = str(uuid5(NAMESPACE_URL, mutation_request_digest(request)))
    result_digest = mutation_result_digest(
        {
            "request_digest": mutation_request_digest(request),
            "authority_digest": _DIGEST,
            "rollback_image_digest": image_digest,
            "expected_repaired_digest": expected_digest,
            "desired_state": {
                "source_record_pk": "replacement",
                "retired_source_record_pks": ["old"],
            },
        }
    )
    unit = RepairUnit(
        "run",
        "unit",
        1,
        0,
        1,
        _DIGEST,
        _DIGEST,
        "applied",
        "inventory",
        "old",
        _DIGEST,
        _DIGEST,
        _DIGEST,
    )
    fence = RepairFence(
        "run", "unit", "fence", 1, 0, 1, "owner", "token", _DIGEST, _DIGEST, "claimed"
    )
    mutation = RepairMutationResult(
        "run",
        "unit",
        mutation_id,
        1,
        0,
        1,
        "owner",
        "token",
        _DIGEST,
        _DIGEST,
        result_digest,
        image_digest,
        _DIGEST,
        image_digest,
        "applied",
    )
    image = RepairRollbackImage(
        "run",
        "unit",
        "image",
        1,
        0,
        1,
        "owner",
        "token",
        _DIGEST,
        _DIGEST,
        image_digest,
        expected_digest,
        _DIGEST,
        image_digest,
        "available",
    )
    return RepairRollbackCommand(
        RepairRollbackAuthorization(
            unit,
            fence,
            mutation,
            image,
            "review",
            "review-token",
            "approved-predecessor",
            "reviewed_rollback_v1",
            "rollback-authorization-a",
        )
    )


def _bundle() -> RollbackImageBundle:
    relationship: dict[str, JsonValue] = {
        "relationship_type": "LINKED_TO",
        "left_identity": {"key": "source_record_pk", "value": "old"},
        "right_identity": {"key": "person_id", "value": "person"},
        "relationship_properties": {"source_record_pk": "old", "is_active": True},
        "multiplicity_ordinal": 0,
    }
    return RollbackImageBundle(
        "mutation",
        "replacement",
        "old",
        ("old", "child"),
        {"source_record_pk": "old", "lifecycle_status": "active", "is_latest": True},
        (("child", {"source_record_pk": "child", "lifecycle_status": "active"}),),
        (relationship,),
        (),
        {"nodes": [], "relationships": []},
        {},
        "bitrix-crm-deal-1",
        "source-instance",
        "control-instance",
    )


def test_domain_guard_accepts_real_neo4j_record_with_strict_canonical_manifest() -> None:
    manifest = RepairExecutionBoundaryManifest(
        repair_id="repair-domain-guard",
        artifact_id="a" * 32,
        artifact_manifest_hmac="b" * 64,
        inventory_digest="sha256:" + "c" * 64,
        repository_sha="d" * 40,
        image_digest="sha256:" + "e" * 64,
        configuration_digest="sha256:" + "f" * 64,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference="approval-domain-guard",
        unit_ceiling=1,
        stop_conditions=("boundary_drift",),
        source_instance_id="source-instance",
        control_instance_id="control-instance",
        rollback_authority_reference="review",
        rollback_authority_policy="reviewed_rollback_v1",
        graph_boundary_digest=_DIGEST,
        inventory_row_count=1,
        eligible_unit_count=1,
        negative_control_count=0,
    )
    source_record_pks_json = canonical_json_bytes({"source_record_pks": ["old"]}).decode("utf-8")
    shared: dict[str, JsonValue] = {
        "manifest_digest": manifest.manifest_digest,
        "artifact_id": manifest.artifact_id,
        "artifact_manifest_hmac": manifest.artifact_manifest_hmac,
        "inventory_digest": manifest.inventory_digest,
        "boundary_digest": manifest.graph_boundary_digest,
        "source_instance_id": manifest.source_instance_id,
        "control_instance_id": manifest.control_instance_id,
        "source_record_pks_json": source_record_pks_json,
        "manifest_json": canonical_json_bytes(manifest.to_dict()).decode("utf-8"),
        "inventory_row_count": 1,
        "eligible_unit_count": 1,
        "negative_control_count": 0,
        "rollback_authority_reference": manifest.rollback_authority_reference,
        "rollback_authority_policy": manifest.rollback_authority_policy,
        "execution_allowed": False,
    }
    run: dict[str, JsonValue] = {
        **shared,
        "repair_id": manifest.repair_id,
        "run_id": str(uuid5(NAMESPACE_URL, manifest.qualification_identity)),
        "qualification_identity": manifest.qualification_identity,
        "status": "qualified",
    }
    record = Record(
        [
            ("run", run),
            ("qualification_link_count", 1),
            ("boundaries", [shared]),
        ]
    )

    stored = _stored_qualification_from_domain_guard(record)
    assert stored.manifest == manifest
    assert stored.source_record_pks == ("old",)


def _disposition(
    command: RepairRollbackCommand,
    outcome: str = "reconciled",
    drift: RepairRollbackDrift | None = None,
    result_digest: str | None = None,
    status_digest: str | None = None,
) -> dict[str, JsonValue]:
    return {
        "run_id": "run",
        "unit_id": "unit",
        "disposition_id": command.disposition_id,
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "owner_id": "owner",
        "control_token": "token",
        "boundary_digest": _DIGEST,
        "subject_fingerprint": command.authorization.image.image_digest,
        "evidence_digest": _DIGEST,
        "payload_digest": command.request_digest,
        "outcome": outcome,
        "rollback_request_digest": command.request_digest,
        "authorization_reference": command.authorization.authorization_reference,
        "authorization_token": command.authorization.authorization_token,
        "authorization_transition_id": command.authorization.authorization_transition_id,
        "predecessor_transition_id": command.authorization.predecessor_transition_id,
        "authorization_policy": command.authorization.authorization_policy,
        "rollback_image_id": "image",
        "result_digest": result_digest or _DIGEST,
        "drift_total_mismatch_count": 0 if drift is None else drift.total_mismatch_count,
        "drift_summaries_json": "[]"
        if drift is None
        else json.dumps(
            [{"identity": identity, "reason": reason} for identity, reason in drift.summaries],
            sort_keys=True,
            separators=(",", ":"),
        ),
        "drift_complete_digest": None if drift is None else drift.complete_digest,
        "rollback_status_digest": status_digest or _DIGEST,
    }


def _persisted_disposition(
    command: RepairRollbackCommand,
    decision: str,
    drift: RepairRollbackDrift | None,
) -> dict[str, JsonValue]:
    state = "restored" if decision == "restored" else "review_required"
    outcome = "reconciled" if decision == "restored" else "review_required"
    result_digest = build_rollback_result_digest(command, decision, state, drift)
    status_digest = build_rollback_status_digest(
        command, state, command.disposition_id, decision, result_digest, drift
    )
    return _disposition(command, outcome, drift, result_digest, status_digest)


def _terminal_row(
    command: RepairRollbackCommand,
    state: str,
    disposition: object,
    *,
    fence_state: str = "claimed",
) -> dict[str, object]:
    payload_json, request, _, _ = _payload()
    auth = command.authorization
    desired_state: dict[str, JsonValue] = {
        "source_record_pk": "replacement",
        "retired_source_record_pks": ["old"],
    }
    mutation_digest = mutation_result_digest(
        {
            "request_digest": mutation_request_digest(request),
            "authority_digest": _DIGEST,
            "rollback_image_digest": auth.image.image_digest,
            "expected_repaired_digest": auth.image.expected_repaired_digest,
            "desired_state": desired_state,
        }
    )
    checkpoint_digest = object_digest(
        b"crm-deal-identity-repair-checkpoint-v1" + bytes([0]),
        {"result_digest": mutation_digest},
    )
    outbox_digest = outbox_event_digest(
        {
            "run_id": "run",
            "unit_id": "unit",
            "mutation_id": auth.mutation.mutation_id,
            "result_digest": mutation_digest,
        }
    )
    terminal = "restored" if state == "restored" else "reviewed_compensation_required"
    drift = (
        None if state == "restored" else RepairRollbackDrift.from_rows((("source:old", "missing"),))
    )
    result_digest = build_rollback_result_digest(command, terminal, state, drift)
    status_digest = build_rollback_status_digest(
        command, state, command.disposition_id, terminal, result_digest, drift
    )
    disposition_values = _disposition(
        command,
        "reconciled" if state == "restored" else "review_required",
        drift,
        result_digest,
        status_digest,
    )
    if isinstance(disposition, dict):
        disposition_values.update(cast(dict[str, JsonValue], disposition))
    return {
        "unit": {
            "run_id": "run",
            "unit_id": "unit",
            "generation": 1,
            "sequence": 0,
            "attempt": 1,
            "boundary_digest": _DIGEST,
            "inventory_fingerprint": _DIGEST,
            "state": "rolled_back" if state == "restored" else "review_required",
            "inventory_key": "inventory",
            "source_record_pk": "old",
            "inventory_graph_fingerprint": _DIGEST,
            "inventory_stored_payload_fingerprint": _DIGEST,
            "inventory_binding_digest": _DIGEST,
        },
        "fence": {
            "run_id": "run",
            "unit_id": "unit",
            "fence_id": "fence",
            "generation": 1,
            "sequence": 0,
            "attempt": 1,
            "owner_id": "owner",
            "token": "token",
            "boundary_digest": _DIGEST,
            "fence_fingerprint": _DIGEST,
            "state": fence_state,
        },
        "result": {
            "run_id": "run",
            "unit_id": "unit",
            "mutation_id": auth.mutation.mutation_id,
            "generation": 1,
            "sequence": 0,
            "attempt": 1,
            "owner_id": "owner",
            "fence_token": "token",
            "boundary_digest": _DIGEST,
            "unit_fingerprint": _DIGEST,
            "result_digest": mutation_digest,
            "rollback_image_digest": auth.image.image_digest,
            "evidence_digest": _DIGEST,
            "payload_digest": auth.mutation.payload_digest,
            "outcome": "applied",
            "rollback_image_id": "image",
            "checkpoint_id": "checkpoint",
            "outbox_event_id": "outbox",
            "new_source_record_pk": "replacement",
            "request_digest": mutation_request_digest(request),
            "repaired_state_digest": auth.image.expected_repaired_digest,
        },
        "image": {
            "run_id": "run",
            "unit_id": "unit",
            "rollback_image_id": "image",
            "generation": 1,
            "sequence": 0,
            "attempt": 1,
            "owner_id": "owner",
            "fence_token": "token",
            "boundary_digest": _DIGEST,
            "source_fingerprint": _DIGEST,
            "image_digest": auth.image.image_digest,
            "expected_repaired_digest": auth.image.expected_repaired_digest,
            "evidence_digest": _DIGEST,
            "payload_digest": auth.image.payload_digest,
            "state": state,
            "payload_json": payload_json,
            "rollback_result_digest": result_digest,
            "rollback_status_digest": status_digest,
        },
        "authorization": {
            "run_id": "run",
            "unit_id": "unit",
            "authorization_transition_id": auth.authorization_transition_id,
            "authorization_reference": auth.authorization_reference,
            "authorization_token": auth.authorization_token,
            "predecessor_transition_id": auth.predecessor_transition_id,
            "authorization_policy": auth.authorization_policy,
            "generation": 1,
            "sequence": 0,
            "attempt": 1,
            "boundary_digest": _DIGEST,
            "fence_id": "fence",
            "owner_id": "owner",
            "fence_token": "token",
            "mutation_id": auth.mutation.mutation_id,
            "rollback_image_id": "image",
            "image_digest": auth.image.image_digest,
            "state": "approved" if state == "available" else "consumed",
            "consumable": state == "available",
            **(
                {}
                if state == "available"
                else {
                    "consumed_disposition_id": command.disposition_id,
                    "consumed_request_digest": command.request_digest,
                    "consumed_result_digest": result_digest,
                }
            ),
        },
        "checkpoints": [
            {
                "run_id": "run",
                "unit_id": "unit",
                "checkpoint_id": "checkpoint",
                "generation": 1,
                "sequence": 0,
                "attempt": 1,
                "owner_id": "owner",
                "fence_token": "token",
                "boundary_digest": _DIGEST,
                "checkpoint_digest": checkpoint_digest,
                "evidence_digest": _DIGEST,
                "state": "written",
            }
        ],
        "outboxes": [
            {
                "run_id": "run",
                "unit_id": "unit",
                "event_id": "outbox",
                "generation": 1,
                "sequence": 0,
                "attempt": 1,
                "owner_id": "owner",
                "delivery_token": "token",
                "boundary_digest": _DIGEST,
                "mutation_id": auth.mutation.mutation_id,
                "payload_digest": outbox_digest,
                "evidence_digest": _DIGEST,
                "state": "pending",
            }
        ],
        "dispositions": ([] if state == "available" else [disposition_values]),
    }


def _repository(
    transaction: _Transaction, failpoint: Callable[[RollbackFailureStage], None] | None = None
) -> CrmDealIdentityRepairRollbackRepository:
    return CrmDealIdentityRepairRollbackRepository(
        cast(Neo4jClient, _Client(transaction)), failpoint=failpoint
    )


def _decoded_bundle(
    row: dict[str, object], command: RepairRollbackCommand
) -> tuple[
    CrmDealIdentityRepairRollbackRepository, RepairRollbackAuthorization, RollbackImageBundle
]:
    repository = _repository(_Transaction({}))
    authorization, payload = repository._authorization_from_row(cast(Record, row), command)
    bundle = decode_rollback_image(
        authorization.image,
        authorization.mutation,
        payload,
        cast(dict[str, JsonValue], row["result"])["request_digest"],
    )
    return repository, authorization, bundle


def _stub_exact_path(
    monkeypatch: pytest.MonkeyPatch, repository: CrmDealIdentityRepairRollbackRepository
) -> None:
    command = _command()
    monkeypatch.setattr(
        repository, "_authorization_from_row", lambda _row, _command: (command.authorization, "{}")
    )
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_rollback.decode_rollback_image", lambda *_: _bundle()
    )
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_rollback.compare_current_state", lambda *_: None
    )
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_rollback.normalize_post_rollback_state",
        lambda value: value,
    )
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_rollback.desired_post_rollback_state",
        lambda *_: {"nodes": [], "relationships": []},
    )
    monkeypatch.setattr(repository, "_domain_guard", lambda *_: None)
    monkeypatch.setattr(repository, "_assert_decoded_bundle_bindings", lambda *_: None)
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_rollback.postcondition_history_matches",
        lambda *_: True,
    )


def test_exact_restore_commits_one_fenced_transaction_with_replacement_restoration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    transaction = _Transaction(
        {
            queries.READ_ROLLBACK_TERMINAL: [None],
            queries.LOCK_AND_READ_ROLLBACK_BUNDLE: [_terminal_row(command, "available", None)],
            queries.READ_ROLLBACK_CURRENT_STATE: [{"nodes": [], "relationships": []}],
            queries.RESTORE_ORIGINAL_SOURCE: [{"restored_count": 2}],
            queries.RESTORE_PREEXISTING_RELATIONSHIPS: [{"restored_count": 1}],
            queries.MAKE_MUTATION_EVIDENCE_HISTORICAL: [{"source_record_pk": "replacement"}],
            queries.READ_ROLLBACK_POSTCONDITION: [
                {
                    "sources": [],
                    "relationships": [],
                    "replacement": {},
                    "mutation_nodes": [],
                    "mutation_relationships": [],
                }
            ],
            queries.PERSIST_ROLLBACK_TERMINAL: [
                {
                    "disposition": _persisted_disposition(command, "restored", None),
                    "authorization": {**_terminal_row(command, "restored", None)["authorization"]},
                }
            ],
        }
    )
    repository = _repository(transaction)
    _stub_exact_path(monkeypatch, repository)

    result = repository.commit_atomic_rollback(command)

    assert result.decision == "restored"
    assert result.image_state == "restored"
    assert [query for query, _ in transaction.calls] == [
        queries.READ_ROLLBACK_TERMINAL,
        queries.LOCK_AND_READ_ROLLBACK_BUNDLE,
        queries.READ_ROLLBACK_TERMINAL,
        queries.READ_ROLLBACK_CURRENT_STATE,
        queries.RESTORE_ORIGINAL_SOURCE,
        queries.RESTORE_PREEXISTING_RELATIONSHIPS,
        queries.MAKE_MUTATION_EVIDENCE_HISTORICAL,
        queries.READ_ROLLBACK_POSTCONDITION,
        queries.PERSIST_ROLLBACK_TERMINAL,
    ]
    restored_sources = transaction.calls[4][1]["sources"]
    assert restored_sources == [
        {"source_record_pk": "old", "properties": _bundle().source_properties},
        {"source_record_pk": "child", "properties": _bundle().descendant_properties[0][1]},
    ]
    terminal = transaction.calls[-1][1]
    assert terminal["unit_state"] == "rolled_back"
    assert terminal["image_state"] == "restored"


def test_valid_authority_drift_persists_only_reviewed_compensation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command = _command()
    drift = RepairRollbackDrift.from_rows((("nodes:source_record_pk:old:0", "missing"),))
    transaction = _Transaction(
        {
            queries.READ_ROLLBACK_TERMINAL: [None],
            queries.LOCK_AND_READ_ROLLBACK_BUNDLE: [_terminal_row(command, "available", None)],
            queries.READ_ROLLBACK_CURRENT_STATE: [{"nodes": [], "relationships": []}],
            queries.PERSIST_ROLLBACK_TERMINAL: [
                {
                    "disposition": _persisted_disposition(
                        command, "reviewed_compensation_required", drift
                    ),
                    "authorization": {
                        **_terminal_row(command, "review_required", None)["authorization"],
                        "consumed_result_digest": build_rollback_result_digest(
                            command, "reviewed_compensation_required", "review_required", drift
                        ),
                    },
                }
            ],
        }
    )
    repository = _repository(transaction)
    _stub_exact_path(monkeypatch, repository)
    monkeypatch.setattr(
        "src.graph.crm_deal_identity_repair_rollback.compare_current_state", lambda *_: drift
    )

    result = repository.commit_atomic_rollback(command)

    assert result.decision == "reviewed_compensation_required"
    assert result.drift == drift
    assert queries.RESTORE_ORIGINAL_SOURCE not in [query for query, _ in transaction.calls]
    terminal = transaction.calls[-1][1]
    assert terminal["unit_state"] == "review_required"
    assert terminal["outcome"] == "review_required"


@pytest.mark.parametrize(
    "missing_query", [queries.READ_ROLLBACK_TERMINAL, queries.LOCK_AND_READ_ROLLBACK_BUNDLE]
)
def test_stale_or_missing_authority_performs_no_restore_or_terminal_write(
    missing_query: str,
) -> None:
    transaction = _Transaction({missing_query: [None]})
    repository = _repository(transaction)

    with pytest.raises(RepairRollbackAuthorityError):
        repository.commit_atomic_rollback(_command())

    writes = {
        queries.RESTORE_ORIGINAL_SOURCE,
        queries.RESTORE_PREEXISTING_RELATIONSHIPS,
        queries.PERSIST_ROLLBACK_TERMINAL,
    }
    assert not writes.intersection(query for query, _ in transaction.calls)


@pytest.mark.parametrize(
    "state,outcome", [("restored", "reconciled"), ("review_required", "review_required")]
)
def test_terminal_replays_are_deterministic_and_do_not_rewrite(
    state: str,
    outcome: str,
) -> None:
    command = _command()
    transaction = _Transaction(
        {queries.READ_ROLLBACK_TERMINAL: [_terminal_row(command, state, None)]}
    )

    result = _repository(transaction).commit_atomic_rollback(command)
    assert result.decision == "replayed"
    assert result.original_terminal_decision == (
        "restored" if state == "restored" else "reviewed_compensation_required"
    )
    assert (result.drift is None) is (state == "restored")
    assert len(transaction.calls) == 1


@pytest.mark.parametrize("terminal_state", ("restored", "review_required"))
@pytest.mark.parametrize("fence_state", ("released", "lost"))
def test_terminal_status_and_replay_allow_completed_fence_lifecycle(
    terminal_state: str,
    fence_state: str,
) -> None:
    command = _command()
    row = _terminal_row(command, terminal_state, None, fence_state=fence_state)
    transaction = _Transaction(
        {
            queries.READ_ROLLBACK_TERMINAL: [
                row,
                _terminal_row(command, terminal_state, None, fence_state=fence_state),
            ]
        }
    )
    client = _Client(transaction)
    repository = CrmDealIdentityRepairRollbackRepository(cast(Neo4jClient, client))

    status = repository.get_rollback_status(command)
    replay = repository.commit_atomic_rollback(command)

    expected_original = (
        "restored" if terminal_state == "restored" else "reviewed_compensation_required"
    )
    assert status.image_state == terminal_state
    assert replay.decision == "replayed"
    assert replay.original_terminal_decision == expected_original
    assert client.read_calls == 1
    assert client.write_calls == 1
    assert [query for query, _ in transaction.calls] == [
        queries.READ_ROLLBACK_TERMINAL,
        queries.READ_ROLLBACK_TERMINAL,
    ]


def test_available_image_rejects_released_fence_before_any_terminal_write() -> None:
    command = _command()
    row = _terminal_row(command, "available", None, fence_state="released")
    transaction = _Transaction({queries.READ_ROLLBACK_TERMINAL: [row]})

    with pytest.raises(RepairRollbackAuthorityError, match="no longer claimed"):
        _repository(transaction).get_rollback_status(command)

    assert queries.PERSIST_ROLLBACK_TERMINAL not in [query for query, _ in transaction.calls]


def test_terminal_replay_rejects_changed_immutable_fence_fingerprint() -> None:
    command = _command()
    row = _terminal_row(command, "restored", None, fence_state="lost")
    cast(dict[str, JsonValue], row["fence"])["fence_fingerprint"] = "sha256:" + "b" * 64

    with pytest.raises(RepairRollbackDriftError, match="immutable fence"):
        _repository(_Transaction({queries.READ_ROLLBACK_TERMINAL: [row]})).get_rollback_status(
            command
        )


def test_terminal_bundle_cardinality_and_changed_transition_are_rejected() -> None:
    command = _command()
    cardinality = _terminal_row(command, "restored", None)
    disposition = cast(dict[str, JsonValue], cardinality["dispositions"][0])
    cardinality["dispositions"] = [disposition, dict(disposition)]
    with pytest.raises(RepairRollbackDriftError, match="cardinality"):
        _repository(
            _Transaction({queries.READ_ROLLBACK_TERMINAL: [cardinality]})
        ).commit_atomic_rollback(command)

    wrong: dict[str, JsonValue] = {"disposition_id": "another-transition"}
    with pytest.raises(RepairRollbackDriftError, match="consumed_disposition_id"):
        _repository(
            _Transaction(
                {queries.READ_ROLLBACK_TERMINAL: [_terminal_row(command, "restored", wrong)]}
            )
        ).commit_atomic_rollback(command)


def test_status_rejects_tampered_terminal_bundle_and_validates_available_state() -> None:
    command = _command()
    valid = _terminal_row(command, "available", None)
    assert (
        _repository(_Transaction({queries.READ_ROLLBACK_TERMINAL: [valid]}))
        .get_rollback_status(command)
        .image_state
        == "available"
    )

    tampered = _terminal_row(command, "restored", None)
    cast(dict[str, JsonValue], tampered["result"])["rollback_image_digest"] = "sha256:" + "b" * 64
    with pytest.raises(RepairRollbackDriftError, match="malformed"):
        _repository(_Transaction({queries.READ_ROLLBACK_TERMINAL: [tampered]})).get_rollback_status(
            command
        )


@pytest.mark.parametrize(
    ("record_name", "property_name", "value", "message"),
    [
        ("result", "result_digest", "sha256:" + "b" * 64, "result digest differs"),
        ("result", "evidence_digest", "sha256:" + "b" * 64, "evidence_digest"),
        ("image", "evidence_digest", "sha256:" + "b" * 64, "evidence_digest"),
        ("checkpoint", "checkpoint_digest", "sha256:" + "b" * 64, "checkpoint_digest"),
        ("outbox", "payload_digest", "sha256:" + "b" * 64, "payload_digest"),
        ("outbox", "delivery_token", "other-token", "token differs"),
        ("result", "checkpoint_id", "other-checkpoint", "checkpoint_id"),
        ("result", "outbox_event_id", "other-outbox", "outbox_event_id"),
    ],
)
def test_complete_immutable_bundle_rejects_digest_and_child_id_tampering(
    record_name: str, property_name: str, value: str, message: str
) -> None:
    command = _command()
    row = _terminal_row(command, "available", None)
    repository, authorization, bundle = _decoded_bundle(row, command)
    record_name_to_values: dict[str, dict[str, JsonValue]] = {
        "result": cast(dict[str, JsonValue], row["result"]),
        "image": cast(dict[str, JsonValue], row["image"]),
        "checkpoint": cast(dict[str, JsonValue], cast(list[object], row["checkpoints"])[0]),
        "outbox": cast(dict[str, JsonValue], cast(list[object], row["outboxes"])[0]),
    }
    record_name_to_values[record_name][property_name] = value
    if record_name == "result" and property_name == "result_digest":
        result = record_name_to_values["result"]
        result_digest = cast(str, result["result_digest"])
        record_name_to_values["checkpoint"]["checkpoint_digest"] = object_digest(
            b"crm-deal-identity-repair-checkpoint-v1" + bytes([0]),
            {"result_digest": result_digest},
        )
        record_name_to_values["outbox"]["payload_digest"] = outbox_event_digest(
            {
                "run_id": "run",
                "unit_id": "unit",
                "mutation_id": command.authorization.mutation.mutation_id,
                "result_digest": result_digest,
            }
        )

    with pytest.raises(RepairRollbackDriftError, match=message):
        repository._assert_bundle_cross_records(cast(Record, row), authorization)
        repository._assert_decoded_bundle_bindings(cast(Record, row), authorization, bundle)


def test_complete_immutable_bundle_binds_evidence_to_payload_authority_digest() -> None:
    command = _command()
    row = _terminal_row(command, "available", None)
    repository, authorization, bundle = _decoded_bundle(row, command)
    payload = cast(dict[str, JsonValue], bundle.payload["payload"])
    authority_context = cast(dict[str, JsonValue], payload["authority_context"])
    authority_context["authority_digest"] = "sha256:" + "b" * 64

    with pytest.raises(RepairRollbackDriftError, match="evidence_digest"):
        repository._assert_decoded_bundle_bindings(cast(Record, row), authorization, bundle)


@pytest.mark.parametrize(
    "stage",
    [
        "after_guard",
        "after_lock",
        "after_compare",
        "after_restore",
        "after_postcondition",
        "after_ledger",
    ],
)
def test_every_failpoint_escapes_the_callback_before_later_writes(
    monkeypatch: pytest.MonkeyPatch,
    stage: RollbackFailureStage,
) -> None:
    command = _command()
    transaction = _Transaction(
        {
            queries.READ_ROLLBACK_TERMINAL: [None],
            queries.LOCK_AND_READ_ROLLBACK_BUNDLE: [_terminal_row(command, "available", None)],
            queries.READ_ROLLBACK_CURRENT_STATE: [{"nodes": [], "relationships": []}],
            queries.RESTORE_ORIGINAL_SOURCE: [{"restored_count": 2}],
            queries.RESTORE_PREEXISTING_RELATIONSHIPS: [{"restored_count": 1}],
            queries.MAKE_MUTATION_EVIDENCE_HISTORICAL: [{"source_record_pk": "replacement"}],
            queries.READ_ROLLBACK_POSTCONDITION: [
                {
                    "sources": [],
                    "relationships": [],
                    "replacement": {},
                    "mutation_nodes": [],
                    "mutation_relationships": [],
                }
            ],
            queries.PERSIST_ROLLBACK_TERMINAL: [
                {
                    "disposition": _persisted_disposition(command, "restored", None),
                    "authorization": {**_terminal_row(command, "restored", None)["authorization"]},
                }
            ],
        }
    )

    def fail(observed: RollbackFailureStage) -> None:
        if observed == stage:
            raise RuntimeError(stage)

    repository = _repository(transaction, fail)
    _stub_exact_path(monkeypatch, repository)
    with pytest.raises(RuntimeError, match=stage):
        repository.commit_atomic_rollback(command)
    calls = [query for query, _ in transaction.calls]
    if stage != "after_ledger":
        assert calls.count(queries.PERSIST_ROLLBACK_TERMINAL) == 0
