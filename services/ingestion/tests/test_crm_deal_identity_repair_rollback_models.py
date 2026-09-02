"""Pure contracts for #312 fenced rollback identities and bounded evidence."""

from __future__ import annotations

import json
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

import pytest
from src.crm_deal_identity_repair.digests import (
    mutation_request_digest,
    repaired_state_digest,
    rollback_image_digest,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairFence,
    RepairMutationResult,
    RepairRollbackImage,
    RepairUnit,
)
from src.crm_deal_identity_repair.rollback_models import (
    RepairRollbackAuthorization,
    RepairRollbackCommand,
    RepairRollbackDrift,
    RepairRollbackResult,
)
from src.graph.crm_deal_identity_repair_rollback_image import (
    RepairRollbackIntegrityError,
    _validate_operations,
    decode_rollback_image,
)
from src.models import JsonValue

_DIGEST = "sha256:" + "a" * 64


def _authorization() -> RepairRollbackAuthorization:
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
        "mutation",
        1,
        0,
        1,
        "owner",
        "token",
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
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
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        "available",
    )
    return RepairRollbackAuthorization(
        unit,
        fence,
        mutation,
        image,
        "review-ref",
        "review-token",
        "approved-predecessor",
        "reviewed_rollback_v1",
        "rollback-authorization-a",
    )


def test_command_identity_binds_every_authority_field_and_is_deterministic() -> None:
    command = RepairRollbackCommand(_authorization())
    assert command.disposition_id == RepairRollbackCommand(_authorization()).disposition_id
    assert command.request_digest.startswith("sha256:")


@pytest.mark.parametrize("fence_state", ("released", "lost"))
def test_authorization_rejects_non_consumable_fence_lifecycle(
    fence_state: str,
) -> None:
    auth = _authorization()
    with pytest.raises(ValueError, match="consumable"):
        RepairRollbackAuthorization(
            auth.unit,
            RepairFence(
                "run", "unit", "fence", 1, 0, 1, "owner", "token", _DIGEST, _DIGEST, fence_state
            ),
            auth.mutation,
            auth.image,
            "review-ref",
            "review-token",
            "verification-1",
            "reviewed_rollback_v1",
            "rollback-authorization-a",
        )


def test_authorization_rejects_cross_image_digest() -> None:
    auth = _authorization()
    with pytest.raises(ValueError, match="image digest"):
        RepairRollbackAuthorization(
            auth.unit,
            auth.fence,
            auth.mutation,
            RepairRollbackImage(
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
                "sha256:" + "b" * 64,
                _DIGEST,
                _DIGEST,
                _DIGEST,
                "available",
            ),
            "review-ref",
            "review-token",
            "verification-1",
            "reviewed_rollback_v1",
            "rollback-authorization-a",
        )


def test_drift_is_sorted_deduplicated_bounded_and_non_sensitive() -> None:
    drift = RepairRollbackDrift.from_rows(
        (("person-b", "missing"), ("person-a", "unexpected"), ("person-b", "missing"))
    )
    assert drift.total_mismatch_count == 2
    assert drift.summaries == (("person-a", "unexpected"), ("person-b", "missing"))
    assert "person-a" not in drift.complete_digest
    with pytest.raises(ValueError, match="bounds"):
        RepairRollbackDrift(1, tuple((str(index), "changed") for index in range(21)), _DIGEST)


def test_rejected_result_cannot_claim_disposition_or_drift() -> None:
    with pytest.raises(ValueError, match="rejected"):
        RepairRollbackResult(
            "rejected",
            "available",
            _DIGEST,
            drift=RepairRollbackDrift.from_rows((("source", "missing"),)),
        )


def test_v1_image_uses_canonical_duplicate_multiset_not_hidden_relationship_ids() -> None:
    from src.graph.crm_deal_identity_repair_rollback_image import RollbackImageBundle
    from src.graph.crm_deal_identity_repair_rollback_state import restoration_ambiguity

    first = {
        "relationship_type": "LINKED_TO",
        "left_identity": {"key": "source_record_pk", "value": "old"},
        "right_identity": {"key": "person_id", "value": "person"},
        "relationship_properties": {
            "source_record_pk": "old",
            "is_active": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
        },
        "multiplicity_ordinal": 0,
    }
    second = {
        **first,
        "relationship_properties": {
            "source_record_pk": "old",
            "is_active": True,
            "updated_at": "2026-01-02T00:00:00+00:00",
        },
        "multiplicity_ordinal": 1,
    }
    bundle = RollbackImageBundle(
        "mutation",
        "replacement",
        "old",
        ("old",),
        {},
        (),
        (first, second),
        (),
        {},
        {},
        "record",
        "source-instance",
        "control-instance",
    )
    drift = restoration_ambiguity(bundle)
    assert drift is None


def _bundle() -> object:
    from src.graph.crm_deal_identity_repair_rollback_image import RollbackImageBundle

    relation = {
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
        (relation,),
        (),
        {"nodes": [], "relationships": []},
        {"payload": {"pre_state": {"source": {"source_record_pk": "old"}}}},
        "record",
        "source-instance",
        "control-instance",
    )


def test_expected_current_and_desired_post_state_detect_property_and_multiplicity_drift() -> None:
    from src.graph.crm_deal_identity_repair_rollback_state import (
        compare_current_state,
        desired_post_rollback_state,
        expected_current_state,
    )

    bundle = _bundle()
    assert hasattr(bundle, "source_record_pk")
    expected = expected_current_state(bundle)  # type: ignore[arg-type]
    assert expected["nodes"]
    drift = compare_current_state(bundle, {"nodes": [], "relationships": []})  # type: ignore[arg-type]
    assert drift is not None
    desired = desired_post_rollback_state(bundle, "rollback")  # type: ignore[arg-type]
    assert len(desired["nodes"]) == 2
    assert len(desired["relationships"]) == 1


def test_current_state_uses_one_v309_schema_for_mutation_owned_topology() -> None:
    """A real #309-style FROM_SOURCE/AUDIT projection must not false-drift."""
    from src.graph.crm_deal_identity_repair_rollback_image import RollbackImageBundle
    from src.graph.crm_deal_identity_repair_rollback_state import compare_current_state

    source = {
        "source_record_pk": "old",
        "lifecycle_status": "active",
        "is_latest": True,
    }
    replacement = {
        "source_record_pk": "replacement",
        "repair_mutation_id": "mutation",
        "is_latest": True,
        "created_at": {"dynamic": "transaction_datetime"},
    }
    decision = {
        "match_decision_id": "mutation:decision",
        "repair_mutation_id": "mutation",
        "created_at": {"dynamic": "transaction_datetime"},
    }
    created_relationships: list[dict[str, object]] = [
        {
            "object_kind": "FROM_SOURCE",
            "direction": "outgoing",
            "left_endpoint": {"source_record_pk": "replacement"},
            "right_endpoint": {"source_key": "bitrix_chat"},
            "properties": {"repair_mutation_id": "mutation"},
        },
        {
            "object_kind": "ABOUT_LEFT",
            "direction": "outgoing",
            "left_endpoint": {"match_decision_id": "mutation:decision"},
            "right_endpoint": {"source_record_pk": "replacement"},
            "properties": {"repair_mutation_id": "mutation"},
        },
    ]
    bundle = RollbackImageBundle(
        "mutation",
        "replacement",
        "old",
        ("old",),
        source,
        (),
        (),
        (),
        {
            "nodes": [
                {
                    "object_kind": "SourceRecord",
                    "identity": {"source_record_pk": "replacement"},
                    "properties": replacement,
                },
                {
                    "object_kind": "MatchDecision",
                    "identity": {"match_decision_id": "mutation:decision"},
                    "properties": decision,
                },
            ],
            "relationships": created_relationships,
        },
        {},
        "record",
        "source-instance",
        "control-instance",
    )
    observed = {
        "nodes": [
            {
                "object_kind": "SourceRecord",
                "identity": {"source_record_pk": "old"},
                "properties": {
                    **source,
                    "lifecycle_status": "superseded",
                    "is_latest": False,
                    "superseded_at": "2026-01-01T00:00:00+00:00",
                },
            },
            {
                "object_kind": "SourceRecord",
                "identity": {"source_record_pk": "replacement"},
                "properties": {**replacement, "created_at": "2026-01-01T00:00:00+00:00"},
            },
            {
                "object_kind": "MatchDecision",
                "identity": {"match_decision_id": "mutation:decision"},
                "properties": {**decision, "created_at": "2026-01-01T00:00:00+00:00"},
            },
        ],
        "relationships": [
            {
                "direction": "outgoing",
                "left_labels": ["SourceRecord"],
                "left_properties": replacement,
                "relationship_type": "FROM_SOURCE",
                "relationship_properties": {"repair_mutation_id": "mutation"},
                "right_labels": ["SourceSystem"],
                "right_properties": {"source_key": "bitrix_chat"},
            },
            {
                "direction": "outgoing",
                "left_labels": ["MatchDecision"],
                "left_properties": decision,
                "relationship_type": "ABOUT_LEFT",
                "relationship_properties": {"repair_mutation_id": "mutation"},
                "right_labels": ["SourceRecord"],
                "right_properties": replacement,
            },
        ],
    }
    assert compare_current_state(bundle, observed) is None
    malformed_timestamp = deepcopy(observed)
    old_properties = malformed_timestamp["nodes"][0]["properties"]
    assert isinstance(old_properties, dict)
    old_properties["superseded_at"] = "not-a-transaction-datetime"
    assert compare_current_state(bundle, malformed_timestamp) is not None


def test_restore_rows_derives_locator_ordinals_for_property_distinct_duplicates() -> None:
    from src.graph.crm_deal_identity_repair_rollback_restoration import restore_rows

    rows: tuple[dict[str, JsonValue], ...] = (
        {
            "relationship_type": "HAS_FACT",
            "left_identity": {"key": "person_id", "value": "person"},
            "right_identity": {"key": "source_record_pk", "value": "old"},
            "relationship_properties": {"source_record_pk": "old", "attribute_name": "a"},
            "multiplicity_ordinal": 0,
        },
        {
            "relationship_type": "HAS_FACT",
            "left_identity": {"key": "person_id", "value": "person"},
            "right_identity": {"key": "source_record_pk", "value": "old"},
            "relationship_properties": {"source_record_pk": "old", "attribute_name": "b"},
            "multiplicity_ordinal": 0,
        },
    )
    groups = restore_rows(rows)
    assert len(groups) == 1
    assert groups[0]["group_size"] == 2
    assignments = groups[0]["assignments"]
    assert isinstance(assignments, list)
    assert [item["restore_ordinal"] for item in assignments if isinstance(item, dict)] == [0, 1]


def test_existing_309_request_without_mutation_id_decodes_and_binds_request_digest() -> None:
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
    request_digest = mutation_request_digest(request)
    mutation_id = str(uuid5(NAMESPACE_URL, request_digest))
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
    image_digest = rollback_image_digest(payload)
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
        _DIGEST,
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
        repaired_state_digest(expected),
        _DIGEST,
        image_digest,
        "available",
    )
    payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))

    bundle = decode_rollback_image(image, mutation, payload_json, request_digest)
    assert bundle.mutation_id == mutation_id
    assert "mutation_id" not in request
    assert "source_record_id" not in request
    with pytest.raises(RepairRollbackIntegrityError, match="request digest"):
        decode_rollback_image(image, mutation, payload_json, "sha256:" + "b" * 64)


def test_frozen_v1_operations_reject_every_cross_field_tamper() -> None:
    mutation_id = "mutation-1"
    source_record_pk = "old"
    replacement_source_record_pk = "replacement"
    relationships: tuple[dict[str, JsonValue], ...] = (
        {
            "relationship_type": "LINKED_TO",
            "left_identity": {"key": "source_record_pk", "value": source_record_pk},
            "right_identity": {"key": "person_id", "value": "person-1"},
            "relationship_properties": {"source_record_pk": source_record_pk},
            "multiplicity_ordinal": 0,
        },
    )
    created: tuple[dict[str, JsonValue], ...] = (
        {
            "object_kind": "SourceRecord",
            "identity": {"source_record_pk": replacement_source_record_pk},
            "properties": {"repair_mutation_id": mutation_id},
            "preexisting": False,
            "write_mode": "created",
        },
    )
    candidates: tuple[JsonValue, ...] = ({"identifier_key": "candidate-1"},)
    body = _frozen_v1_operations_body(
        mutation_id,
        source_record_pk,
        replacement_source_record_pk,
        created,
        relationships,
        candidates,
    )
    _validate_operations(
        body,
        mutation_id,
        source_record_pk,
        replacement_source_record_pk,
        created,
        relationships,
        candidates,
    )

    mutations: tuple[tuple[int, str, JsonValue], ...] = (
        (0, "relationship_types", ["LINKED_TO"]),
        (1, "source_record_pk", "wrong-replacement"),
        (1, "created_object_specifications", []),
        (1, "identifier_candidates", []),
        (1, "delete_identifier_only_when_preexisting_is_false", False),
        (2, "source_record_pk", "wrong-original"),
        (2, "relationships", []),
        (0, "unexpected", "value"),
    )
    for operation_index, key, value in mutations:
        tampered = deepcopy(body)
        operations = tampered["rollback_operations"]
        assert isinstance(operations, list)
        operation = operations[operation_index]
        assert isinstance(operation, dict)
        operation[key] = value
        with pytest.raises(RepairRollbackIntegrityError):
            _validate_operations(
                tampered,
                mutation_id,
                source_record_pk,
                replacement_source_record_pk,
                created,
                relationships,
                candidates,
            )


def _frozen_v1_operations_body(
    mutation_id: str,
    source_record_pk: str,
    replacement_source_record_pk: str,
    created: tuple[dict[str, JsonValue], ...],
    relationships: tuple[dict[str, JsonValue], ...],
    candidates: tuple[JsonValue, ...],
) -> dict[str, JsonValue]:
    return {
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
                "source_record_pk": replacement_source_record_pk,
                "match_decision_id": mutation_id + ":decision",
                "review_case_id": mutation_id + ":review",
                "identifier_repair_mutation_id": mutation_id,
                "identifier_candidates": list(candidates),
                "created_object_specifications": list(created),
                "delete_identifier_only_when_preexisting_is_false": True,
            },
            {
                "operation": "restore_source_and_relationship_properties",
                "source_record_pk": source_record_pk,
                "relationships": list(relationships),
            },
        ]
    }
