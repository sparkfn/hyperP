"""Strict, bounded decoding of the immutable #309 executable rollback image."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import cast
from uuid import NAMESPACE_URL, uuid5

from src.crm_deal_identity_repair.digests import (
    mutation_request_digest,
    repaired_state_digest,
    rollback_image_digest,
)
from src.crm_deal_identity_repair.execution_models import RepairMutationResult, RepairRollbackImage
from src.models import JsonValue

_MAX_IMAGE_BYTES = 1_000_000
_MAX_ROWS = 2_000
_CREATED_RELATIONSHIPS = frozenset(
    {
        "LINKED_TO",
        "IDENTIFIED_BY",
        "LIVES_AT",
        "HAS_FACT",
        "DESCRIBES_ADDRESS",
        "OWNED_BY",
        "FROM_SOURCE",
        "PREVIOUS_VERSION_OF",
        "ABOUT_LEFT",
        "ABOUT_RIGHT",
        "FOR_DECISION",
    }
)
_RELATIONSHIP_TYPE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_V1_CREATED_RELATIONSHIP_TYPES = (
    "LINKED_TO",
    "ABOUT_LEFT",
    "ABOUT_RIGHT",
    "FOR_DECISION",
    "IDENTIFIED_BY",
    "HAS_FACT",
    "FROM_SOURCE",
    "PREVIOUS_VERSION_OF",
    "OWNED_BY",
)


class RepairRollbackIntegrityError(RuntimeError):
    """Immutable repair ledger state cannot safely be interpreted as business drift."""


@dataclass(frozen=True)
class RollbackImageBundle:
    """Validated immutable image fields sufficient for CAS and fixed restoration."""

    mutation_id: str
    replacement_source_record_pk: str
    source_record_pk: str
    retired_source_record_pks: tuple[str, ...]
    source_properties: dict[str, JsonValue]
    descendant_properties: tuple[tuple[str, dict[str, JsonValue]], ...]
    relationship_rows: tuple[dict[str, JsonValue], ...]
    created_specifications: tuple[dict[str, JsonValue], ...]
    expected_repaired_state: dict[str, JsonValue]
    payload: dict[str, JsonValue]
    source_record_id: str
    source_instance_id: str
    control_instance_id: str


def decode_rollback_image(
    image: RepairRollbackImage,
    mutation: RepairMutationResult,
    payload_json: str,
    result_request_digest: str,
) -> RollbackImageBundle:
    """Verify the exact #309 image/result binding before any graph transition."""
    if len(payload_json.encode("utf-8")) > _MAX_IMAGE_BYTES:
        raise RepairRollbackIntegrityError("rollback image exceeds bounded size")
    payload = _canonical_object(payload_json)
    if rollback_image_digest(payload) != image.image_digest:
        raise RepairRollbackIntegrityError("rollback image digest differs")
    expected = _object(payload, "expected_repaired_state")
    if repaired_state_digest(expected) != image.expected_repaired_digest:
        raise RepairRollbackIntegrityError("rollback expected repaired-state digest differs")
    body = _object(payload, "payload")
    if _string(body, "contract_version") != "crm_deal_identity_repair_mutation_v1":
        raise RepairRollbackIntegrityError("unsupported rollback image contract")
    request = _object(body, "request")
    _validate_request_shape(request)
    request_digest = mutation_request_digest(request)
    if request_digest != result_request_digest:
        raise RepairRollbackIntegrityError("rollback mutation request digest differs")
    if str(uuid5(NAMESPACE_URL, request_digest)) != mutation.mutation_id:
        raise RepairRollbackIntegrityError("rollback mutation identity differs")
    _assert_scope(request, image, mutation)
    source_instance_id = _string(request, "source_instance_id")
    control_instance_id = _string(request, "control_instance_id")
    pre_state = _object(body, "pre_state")
    source = _object(pre_state, "source")
    source_record_pk = _string(source, "source_record_pk")
    source_record_id = _string(source, "source_record_id")
    if _string(source, "source_instance_id") != source_instance_id:
        raise RepairRollbackIntegrityError("rollback request/source instance differs")
    desired = _object(body, "desired_state")
    replacement_source_record_pk = _string(desired, "source_record_pk")
    retired = _string_tuple(desired, "retired_source_record_pks")
    if source_record_pk not in retired:
        raise RepairRollbackIntegrityError("rollback image omits original source from retirement")
    relationships = _object_rows(pre_state, "relationships")
    descendants = _object_rows(pre_state, "descendants")
    descendant_properties = _descendant_properties(descendants, source_record_pk)
    _validate_relationships(relationships)
    created = _object_rows(body, "created_object_specifications")
    _validate_created_specs(created, mutation.mutation_id, replacement_source_record_pk)
    _validate_operations(
        body,
        mutation.mutation_id,
        source_record_pk,
        replacement_source_record_pk,
        created,
        relationships,
        _json_array(pre_state, "created_identifier_candidates"),
    )
    return RollbackImageBundle(
        mutation.mutation_id,
        replacement_source_record_pk,
        source_record_pk,
        retired,
        source,
        descendant_properties,
        relationships,
        created,
        expected,
        payload,
        source_record_id,
        source_instance_id,
        control_instance_id,
    )


def _descendant_properties(
    rows: tuple[dict[str, JsonValue], ...], source_record_pk: str
) -> tuple[tuple[str, dict[str, JsonValue]], ...]:
    result: list[tuple[str, dict[str, JsonValue]]] = []
    identities: set[str] = {source_record_pk}
    for row in rows:
        pk = _string(row, "source_record_pk")
        properties = _object(row, "properties")
        path = row.get("ancestry_path")
        if pk in identities or not isinstance(path, list) or source_record_pk not in path:
            raise RepairRollbackIntegrityError(
                "rollback descendant identity or ancestry is invalid"
            )
        identities.add(pk)
        result.append((pk, properties))
    return tuple(sorted(result))


def _validate_request_shape(request: dict[str, JsonValue]) -> None:
    """Accept exactly the frozen #309 command serialization, without regenerated fields."""
    expected = {
        "run_id",
        "unit_id",
        "generation",
        "sequence",
        "attempt",
        "owner_id",
        "fence_id",
        "fence_token",
        "boundary_digest",
        "unit_fingerprint",
        "inventory_key",
        "inventory_fingerprint",
        "inventory_binding_digest",
        "stored_payload_fingerprint",
        "source_instance_id",
        "control_instance_id",
        "mutation_contract_version",
    }
    if set(request) != expected:
        raise RepairRollbackIntegrityError("rollback request shape differs")
    for key in expected - {"generation", "sequence", "attempt"}:
        _string(request, key)
    for key in {"generation", "sequence", "attempt"}:
        value = request.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RepairRollbackIntegrityError("rollback request numeric field is invalid")
    if request.get("mutation_contract_version") != "crm_deal_identity_repair_mutation_v1":
        raise RepairRollbackIntegrityError("rollback request contract differs")


def _validate_operations(
    body: dict[str, JsonValue],
    mutation_id: str,
    source_record_pk: str,
    replacement_source_record_pk: str,
    created_specifications: tuple[dict[str, JsonValue], ...],
    relationships: tuple[dict[str, JsonValue], ...],
    identifier_candidates: tuple[JsonValue, ...],
) -> None:
    """Bind the fixed #309 rollback-operation evidence without interpreting it as Cypher."""
    value = body.get("rollback_operations")
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(row, dict) for row in value)
    ):
        raise RepairRollbackIntegrityError("rollback operations are invalid")
    operation_rows = cast(list[dict[str, JsonValue]], value)
    rows: dict[str, dict[str, JsonValue]] = {}
    for row in operation_rows:
        operation = row.get("operation")
        if not isinstance(operation, str) or operation in rows:
            raise RepairRollbackIntegrityError("rollback operations are invalid")
        rows[operation] = row
    if set(rows) != {
        "delete_created_relationships_by_repair_mutation_id",
        "delete_created_nodes_and_identifiers",
        "restore_source_and_relationship_properties",
    }:
        raise RepairRollbackIntegrityError("rollback operations are unsupported")
    delete_relationships = rows["delete_created_relationships_by_repair_mutation_id"]
    if (
        set(delete_relationships)
        != {
            "operation",
            "repair_mutation_id",
            "relationship_types",
        }
        or delete_relationships.get("repair_mutation_id") != mutation_id
    ):
        raise RepairRollbackIntegrityError("rollback operation mutation differs")
    relationship_types = delete_relationships.get("relationship_types")
    if not isinstance(relationship_types, list) or tuple(relationship_types) != (
        _V1_CREATED_RELATIONSHIP_TYPES
    ):
        raise RepairRollbackIntegrityError("rollback operation relationship types are invalid")
    delete_nodes = rows["delete_created_nodes_and_identifiers"]
    if (
        set(delete_nodes)
        != {
            "operation",
            "source_record_pk",
            "match_decision_id",
            "review_case_id",
            "identifier_repair_mutation_id",
            "identifier_candidates",
            "created_object_specifications",
            "delete_identifier_only_when_preexisting_is_false",
        }
        or delete_nodes.get("identifier_repair_mutation_id") != mutation_id
    ):
        raise RepairRollbackIntegrityError("rollback operation identifier mutation differs")
    if delete_nodes.get("source_record_pk") != replacement_source_record_pk:
        raise RepairRollbackIntegrityError("rollback operation replacement source differs")
    if delete_nodes.get("match_decision_id") != mutation_id + ":decision":
        raise RepairRollbackIntegrityError("rollback operation decision identity differs")
    if delete_nodes.get("review_case_id") != mutation_id + ":review":
        raise RepairRollbackIntegrityError("rollback operation review identity differs")
    if delete_nodes.get("created_object_specifications") != list(created_specifications):
        raise RepairRollbackIntegrityError("rollback operation created specifications differ")
    if delete_nodes.get("identifier_candidates") != list(identifier_candidates):
        raise RepairRollbackIntegrityError("rollback operation identifier candidates differ")
    if delete_nodes.get("delete_identifier_only_when_preexisting_is_false") is not True:
        raise RepairRollbackIntegrityError("rollback operation identifier policy differs")
    restore = rows["restore_source_and_relationship_properties"]
    if set(restore) != {"operation", "source_record_pk", "relationships"}:
        raise RepairRollbackIntegrityError("rollback restore operation is malformed")
    if restore.get("source_record_pk") != source_record_pk:
        raise RepairRollbackIntegrityError("rollback operation source identity differs")
    if restore.get("relationships") != list(relationships):
        raise RepairRollbackIntegrityError("rollback operation relationships differ")


def _canonical_object(value: str) -> dict[str, JsonValue]:
    try:
        decoded: object = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RepairRollbackIntegrityError("rollback payload is unreadable") from exc
    if not isinstance(decoded, dict) or not all(isinstance(key, str) for key in decoded):
        raise RepairRollbackIntegrityError("rollback payload is not an object")
    canonical = json.dumps(decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    if canonical != value:
        raise RepairRollbackIntegrityError("rollback payload is not canonical")
    return {cast(str, key): _json_value(item) for key, item in decoded.items()}


def _assert_scope(
    request: dict[str, JsonValue], image: RepairRollbackImage, mutation: RepairMutationResult
) -> None:
    values = ("run_id", "unit_id", "generation", "sequence", "attempt", "boundary_digest")
    expected: dict[str, JsonValue] = {
        "run_id": image.run_id,
        "unit_id": image.unit_id,
        "generation": image.generation,
        "sequence": image.sequence,
        "attempt": image.attempt,
        "boundary_digest": image.boundary_digest,
    }
    if any(request.get(key) != expected[key] for key in values):
        raise RepairRollbackIntegrityError("rollback request scope differs")
    if request.get("owner_id") != mutation.owner_id:
        raise RepairRollbackIntegrityError("rollback request owner differs")
    if (
        mutation.payload_digest != image.payload_digest
        or mutation.rollback_image_digest != image.image_digest
    ):
        raise RepairRollbackIntegrityError("rollback result/image binding differs")


def _validate_relationships(rows: tuple[dict[str, JsonValue], ...]) -> None:
    if len(rows) > _MAX_ROWS:
        raise RepairRollbackIntegrityError("rollback relationship count exceeds bound")
    identities: set[str] = set()
    for row in rows:
        relationship_type = _string(row, "relationship_type")
        if _RELATIONSHIP_TYPE.fullmatch(relationship_type) is None:
            raise RepairRollbackIntegrityError("rollback relationship type is unsupported")
        _object(row, "relationship_properties")
        _object(row, "left_identity")
        _object(row, "right_identity")
        ordinal = row.get("multiplicity_ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
            raise RepairRollbackIntegrityError("rollback relationship multiplicity is invalid")
        identity = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if identity in identities:
            raise RepairRollbackIntegrityError("rollback relationship rows are duplicated")
        identities.add(identity)


def _validate_created_specs(
    rows: tuple[dict[str, JsonValue], ...], mutation_id: str, replacement_pk: str
) -> None:
    if not rows or len(rows) > _MAX_ROWS:
        raise RepairRollbackIntegrityError("rollback created-object specifications are invalid")
    has_source = False
    for row in rows:
        kind = _string(row, "object_kind")
        if kind not in _CREATED_RELATIONSHIPS | {
            "SourceRecord",
            "MatchDecision",
            "ReviewCase",
            "Identifier",
            "FROM_SOURCE",
            "PREVIOUS_VERSION_OF",
            "ABOUT_LEFT",
            "ABOUT_RIGHT",
            "FOR_DECISION",
        }:
            raise RepairRollbackIntegrityError("rollback created object kind is unsupported")
        preexisting = row.get("preexisting")
        write_mode = row.get("write_mode")
        created = preexisting is False and write_mode == "created"
        preserved_identifier = (
            kind == "Identifier" and preexisting is True and write_mode == "preserved"
        )
        if not created and not preserved_identifier:
            raise RepairRollbackIntegrityError("rollback object specification is malformed")
        if kind == "SourceRecord":
            identity = _object(row, "identity")
            has_source = _string(identity, "source_record_pk") == replacement_pk
        if preserved_identifier:
            # #309 records pre-existing shared identifiers as evidence only.  Rollback
            # must neither mutate nor deactivate these nodes.
            _object(row, "identity")
            continue
        properties = row.get("properties")
        if isinstance(properties, dict) and properties.get("repair_mutation_id") not in {
            None,
            mutation_id,
        }:
            raise RepairRollbackIntegrityError("rollback created object ownership differs")
    if not has_source:
        raise RepairRollbackIntegrityError("rollback replacement source specification is missing")


def _object(value: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise RepairRollbackIntegrityError("rollback object field is invalid: " + key)
    return item


def _object_rows(value: dict[str, JsonValue], key: str) -> tuple[dict[str, JsonValue], ...]:
    item = value.get(key)
    if not isinstance(item, list) or not all(isinstance(row, dict) for row in item):
        raise RepairRollbackIntegrityError("rollback row collection is invalid: " + key)
    return tuple(cast(dict[str, JsonValue], row) for row in item)


def _json_array(value: dict[str, JsonValue], key: str) -> tuple[JsonValue, ...]:
    item = value.get(key)
    if not isinstance(item, list):
        raise RepairRollbackIntegrityError("rollback array field is invalid: " + key)
    return tuple(item)


def _string(value: dict[str, JsonValue], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RepairRollbackIntegrityError("rollback string field is invalid: " + key)
    return item


def _string_tuple(value: dict[str, JsonValue], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if (
        not isinstance(item, list)
        or not item
        or not all(isinstance(row, str) and row for row in item)
    ):
        raise RepairRollbackIntegrityError("rollback string collection is invalid: " + key)
    result = tuple(cast(str, row) for row in item)
    if len(result) != len(set(result)):
        raise RepairRollbackIntegrityError("rollback string collection has duplicates")
    return result


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {cast(str, key): _json_value(item) for key, item in value.items()}
    raise RepairRollbackIntegrityError("rollback payload contains unsupported value")
