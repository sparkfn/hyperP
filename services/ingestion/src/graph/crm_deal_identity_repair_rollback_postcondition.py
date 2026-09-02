"""Inert mutation-history postcondition checks for #312 rollback."""

from __future__ import annotations

from src.graph.crm_deal_identity_repair_rollback_image import RollbackImageBundle
from src.models import JsonValue


def postcondition_history_matches(
    bundle: RollbackImageBundle,
    rollback_image_id: str,
    replacement: JsonValue,
    mutation_nodes: JsonValue,
    mutation_relationships: JsonValue,
) -> bool:
    """Verify preserved mutation evidence and only the mandated inert lifecycle changes."""
    if not isinstance(replacement, dict):
        return False
    expected_replacement = {
        "source_record_pk": bundle.replacement_source_record_pk,
        "repair_mutation_id": bundle.mutation_id,
        "lifecycle_status": "rolled_back",
        "is_latest": False,
        "link_status": "rolled_back",
        "repair_rollback_id": rollback_image_id,
    }
    if any(replacement.get(key) != value for key, value in expected_replacement.items()):
        return False
    if not isinstance(mutation_nodes, list) or not isinstance(mutation_relationships, list):
        return False
    nodes = [item for item in mutation_nodes if isinstance(item, dict)]
    relationships = [item for item in mutation_relationships if isinstance(item, dict)]
    if len(nodes) != len(mutation_nodes) or len(relationships) != len(mutation_relationships):
        return False
    expected_nodes = _created_node_count(bundle)
    if len(nodes) != expected_nodes:
        return False
    if not _required_mutation_nodes_present(bundle, nodes, rollback_image_id):
        return False
    expected_relationships = _created_relationship_count(bundle)
    if len(relationships) != expected_relationships:
        return False
    return _mutation_relationships_are_inert(bundle, relationships, rollback_image_id)


def _created_node_count(bundle: RollbackImageBundle) -> int:
    return sum(
        1
        for row in bundle.created_specifications
        if row.get("preexisting") is False
        and row.get("write_mode") == "created"
        and row.get("object_kind") in {"SourceRecord", "MatchDecision", "ReviewCase", "Identifier"}
    )


def _created_relationship_count(bundle: RollbackImageBundle) -> int:
    return sum(
        1
        for row in bundle.created_specifications
        if row.get("preexisting") is False
        and row.get("write_mode") == "created"
        and isinstance(row.get("left_endpoint"), dict)
        and isinstance(row.get("right_endpoint"), dict)
    )


def _required_mutation_nodes_present(
    bundle: RollbackImageBundle, nodes: list[dict[str, JsonValue]], rollback_image_id: str
) -> bool:
    source_present = False
    decision_present = False
    review_present = False
    for node in nodes:
        kind = node.get("object_kind")
        properties = node.get("properties")
        if not isinstance(kind, str) or not isinstance(properties, dict):
            return False
        if properties.get("repair_mutation_id") != bundle.mutation_id:
            return False
        if (
            kind == "SourceRecord"
            and properties.get("source_record_pk") == bundle.replacement_source_record_pk
        ):
            source_present = True
        if (
            kind == "MatchDecision"
            and properties.get("match_decision_id") == bundle.mutation_id + ":decision"
        ):
            decision_present = _historical(properties, rollback_image_id)
        if (
            kind == "ReviewCase"
            and properties.get("review_case_id") == bundle.mutation_id + ":review"
        ):
            review_present = _historical(properties, rollback_image_id)
    expected_review = any(
        row.get("object_kind") == "ReviewCase" and row.get("preexisting") is False
        for row in bundle.created_specifications
    )
    return source_present and decision_present and (review_present or not expected_review)


def _historical(properties: dict[str, JsonValue], rollback_image_id: str) -> bool:
    return (
        properties.get("lifecycle_status") == "historical"
        and properties.get("repair_rollback_id") == rollback_image_id
    )


def _mutation_relationships_are_inert(
    bundle: RollbackImageBundle,
    relationships: list[dict[str, JsonValue]],
    rollback_image_id: str,
) -> bool:
    authoritative = {"LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "DESCRIBES_ADDRESS"}
    for relationship in relationships:
        kind = relationship.get("relationship_type")
        properties = relationship.get("relationship_properties")
        if not isinstance(kind, str) or not isinstance(properties, dict):
            return False
        if properties.get("repair_mutation_id") != bundle.mutation_id:
            return False
        if kind in authoritative and (
            properties.get("is_active") is not False
            or properties.get("authoritative") is not False
            or properties.get("repair_rollback_id") != rollback_image_id
        ):
            return False
    return True
