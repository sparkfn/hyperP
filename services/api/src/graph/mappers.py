"""Map raw Neo4j records to Pydantic domain models."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from neo4j.time import DateTime as Neo4jDateTime

from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_float,
    to_int,
    to_iso_or_empty,
    to_iso_or_none,
    to_optional_str,
    to_str,
    to_str_list,
)
from src.types import (
    AddressSummary,
    AuditEvent,
    DownstreamEvent,
    GraphEdge,
    GraphNode,
    KnowsRelationship,
    MatchDecision,
    MatchDecisionSummary,
    Person,
    PersonComparisonEntity,
    PersonConnection,
    PersonGraph,
    PersonStatus,
    ReviewCaseDetail,
    ReviewCaseSummary,
    SharedAddress,
    SharedIdentifier,
    SourceRecord,
)


def _as_dict(value: GraphValue) -> GraphRecord:
    """Coerce a graph value to a dict, returning empty dict for non-dicts/None."""
    if isinstance(value, dict):
        return value
    return {}


def map_address(value: GraphValue) -> AddressSummary | None:
    addr = _as_dict(value)
    if not addr.get("address_id"):
        return None
    return AddressSummary(
        address_id=to_str(addr["address_id"]),
        unit_number=to_optional_str(addr.get("unit_number")),
        street_number=to_optional_str(addr.get("street_number")),
        street_name=to_optional_str(addr.get("street_name")),
        city=to_optional_str(addr.get("city")),
        postal_code=to_optional_str(addr.get("postal_code")),
        country_code=to_optional_str(addr.get("country_code")),
        normalized_full=to_optional_str(addr.get("normalized_full")),
    )


def map_person(record: GraphRecord, address_key: str = "preferred_address") -> Person:
    p = _as_dict(record.get("person"))
    return Person(
        person_id=to_str(p.get("person_id")),
        status=PersonStatus(to_str(p.get("status"), "active")),
        is_high_value=bool(p.get("is_high_value")),
        is_high_risk=bool(p.get("is_high_risk")),
        preferred_full_name=to_optional_str(p.get("preferred_full_name")),
        preferred_phone=to_optional_str(p.get("preferred_phone")),
        preferred_email=to_optional_str(p.get("preferred_email")),
        preferred_dob=to_optional_str(p.get("preferred_dob")),
        preferred_address=map_address(record.get(address_key)),
        preferred_nric=to_optional_str(p.get("preferred_nric")),
        profile_completeness_score=to_float(p.get("profile_completeness_score")),
        golden_profile_computed_at=to_iso_or_none(p.get("golden_profile_computed_at")),
        golden_profile_version=to_optional_str(p.get("golden_profile_version")),
        source_record_count=to_int(record.get("source_record_count")),
        connection_count=to_int(record.get("connection_count")),
        created_at=to_iso_or_empty(p.get("created_at")),
        updated_at=to_iso_or_empty(p.get("updated_at")),
    )


def map_source_record(record: GraphRecord) -> SourceRecord:
    sr = _as_dict(record.get("source_record"))
    return SourceRecord(
        source_record_pk=to_str(sr.get("source_record_pk")),
        source_system=to_str(record.get("source_system")),
        source_record_id=to_str(sr.get("source_record_id")),
        source_record_version=to_optional_str(sr.get("source_record_version")),
        record_type="conversation" if to_str(sr.get("record_type")) == "conversation" else "system",
        extraction_confidence=(
            to_float(sr.get("extraction_confidence"))
            if sr.get("extraction_confidence") is not None
            else None
        ),
        link_status=to_str(sr.get("link_status")),
        linked_person_id=to_optional_str(record.get("linked_person_id")),
        observed_at=to_iso_or_empty(sr.get("observed_at")),
        ingested_at=to_iso_or_empty(sr.get("ingested_at")),
    )


def _map_shared_identifiers(value: GraphValue) -> list[SharedIdentifier]:
    if not isinstance(value, list):
        return []
    return [
        SharedIdentifier(
            identifier_type=to_str(d.get("identifier_type")),
            normalized_value=to_str(d.get("normalized_value")),
        )
        for raw in value if (d := _as_dict(raw)).get("identifier_type")
    ]


def _map_shared_addresses(value: GraphValue) -> list[SharedAddress]:
    if not isinstance(value, list):
        return []
    return [
        SharedAddress(
            address_id=to_str(d.get("address_id")),
            normalized_full=to_optional_str(d.get("normalized_full")),
        )
        for raw in value if (d := _as_dict(raw)).get("address_id")
    ]


def _map_knows_relationships(value: GraphValue) -> list[KnowsRelationship]:
    if not isinstance(value, list):
        return []
    return [
        KnowsRelationship(
            relationship_label=to_optional_str(d.get("relationship_label")),
            relationship_category=to_str(d.get("relationship_category")),
        )
        for raw in value if (d := _as_dict(raw)).get("relationship_category")
    ]


def map_connection(record: GraphRecord) -> PersonConnection:
    return PersonConnection(
        person_id=to_str(record.get("person_id")),
        status=to_str(record.get("status")),
        preferred_full_name=to_optional_str(record.get("preferred_full_name")),
        hops=to_int(record.get("hops")),
        shared_identifiers=_map_shared_identifiers(record.get("shared_identifiers")),
        shared_addresses=_map_shared_addresses(record.get("shared_addresses")),
        knows_relationships=_map_knows_relationships(
            record.get("knows_relationships")
        ),
    )


def map_audit_event(record: GraphRecord) -> AuditEvent:
    me = _as_dict(record.get("merge_event"))
    metadata_raw = me.get("metadata")
    metadata: dict[str, str] = {}
    if isinstance(metadata_raw, dict):
        metadata = {to_str(k): to_str(v) for k, v in metadata_raw.items()}
    return AuditEvent(
        merge_event_id=to_str(me.get("merge_event_id")),
        event_type=to_str(me.get("event_type")),
        actor_type=to_str(me.get("actor_type")),
        actor_id=to_str(me.get("actor_id")),
        reason=to_optional_str(me.get("reason")),
        metadata=metadata,
        created_at=to_iso_or_empty(me.get("created_at")),
        absorbed_person_id=to_optional_str(record.get("absorbed_person_id")),
        survivor_person_id=to_optional_str(record.get("survivor_person_id")),
        triggered_by_decision_id=to_optional_str(record.get("triggered_by_decision_id")),
    )


def map_match_decision(record: GraphRecord) -> MatchDecision:
    md = _as_dict(record.get("match_decision"))
    return MatchDecision(
        match_decision_id=to_str(md.get("match_decision_id")),
        engine_type=to_str(md.get("engine_type")),
        engine_version=to_str(md.get("engine_version")),
        policy_version=to_str(md.get("policy_version")),
        decision=to_str(md.get("decision")),
        confidence=to_float(md.get("confidence")),
        reasons=to_str_list(md.get("reasons")),
        blocking_conflicts=to_str_list(md.get("blocking_conflicts")),
        created_at=to_iso_or_empty(md.get("created_at")),
        left_person_id=to_optional_str(record.get("left_person_id")),
        right_person_id=to_optional_str(record.get("right_person_id")),
    )


def map_review_case_summary(record: GraphRecord) -> ReviewCaseSummary:
    rc = _as_dict(record.get("review_case"))
    md = _as_dict(record.get("match_decision"))
    return ReviewCaseSummary(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        priority=to_int(rc.get("priority")),
        assigned_to=to_optional_str(rc.get("assigned_to")),
        follow_up_at=to_iso_or_none(rc.get("follow_up_at")),
        sla_due_at=to_iso_or_none(rc.get("sla_due_at")),
        match_decision=MatchDecisionSummary(
            match_decision_id=to_str(md.get("match_decision_id")),
            engine_type=to_str(md.get("engine_type")),
            decision=to_str(md.get("decision")),
            confidence=to_float(md.get("confidence")),
        ),
    )


def _map_comparison_entity(
    kind: GraphValue, entity: GraphValue, address: GraphValue
) -> PersonComparisonEntity | None:
    e = _as_dict(entity)
    if not e:
        return None
    kind_str = to_optional_str(kind)
    if kind_str == "source_record":
        return _map_source_record_comparison(e)
    return PersonComparisonEntity(
        entity_kind="person",
        person_id=to_optional_str(e.get("person_id")),
        status=to_optional_str(e.get("status")),
        preferred_full_name=to_optional_str(e.get("preferred_full_name")),
        preferred_phone=to_optional_str(e.get("preferred_phone")),
        preferred_email=to_optional_str(e.get("preferred_email")),
        preferred_dob=to_optional_str(e.get("preferred_dob")),
        preferred_address=map_address(address),
    )


def _map_source_record_comparison(e: GraphRecord) -> PersonComparisonEntity:
    payload = _parse_normalized_payload(e.get("normalized_payload"))
    return PersonComparisonEntity(
        entity_kind="source_record",
        source_record_pk=to_optional_str(e.get("source_record_pk")),
        source_record_id=to_optional_str(e.get("source_record_id")),
        status=None,
        preferred_full_name=_attribute_value(payload, "full_name"),
        preferred_phone=_identifier_value(payload, "phone"),
        preferred_email=_identifier_value(payload, "email"),
        preferred_dob=_attribute_value(payload, "dob"),
        preferred_address=_source_record_address(payload),
    )


def _parse_normalized_payload(value: GraphValue) -> GraphRecord:
    if not isinstance(value, str):
        return {}
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _attribute_value(payload: GraphRecord, name: Literal["full_name", "dob"]) -> str | None:
    attrs = payload.get("attributes")
    if not isinstance(attrs, list):
        return None
    for raw in attrs:
        item = _as_dict(raw)
        if item.get("attribute_name") == name:
            return to_optional_str(item.get("attribute_value"))
    return None


def _identifier_value(
    payload: GraphRecord, identifier_type: Literal["phone", "email"]
) -> str | None:
    ids = payload.get("identifiers")
    if not isinstance(ids, list):
        return None
    for raw in ids:
        item = _as_dict(raw)
        if item.get("identifier_type") == identifier_type:
            return to_optional_str(item.get("normalized_value"))
    return None


def _source_record_address(payload: GraphRecord) -> AddressSummary | None:
    addr = _as_dict(payload.get("address"))
    if not addr:
        return None
    normalized = to_optional_str(addr.get("normalized_full"))
    if normalized is None:
        return None
    return AddressSummary(
        address_id="",
        unit_number=to_optional_str(addr.get("unit_number")),
        street_number=to_optional_str(addr.get("street_number")),
        street_name=to_optional_str(addr.get("street_name")),
        city=to_optional_str(addr.get("city")),
        postal_code=to_optional_str(addr.get("postal_code")),
        country_code=to_optional_str(addr.get("country_code")),
        normalized_full=normalized,
    )


def map_review_case_detail(record: GraphRecord) -> ReviewCaseDetail:
    rc = _as_dict(record.get("review_case"))
    actions_raw = rc.get("actions")
    actions: list[dict[str, str | None]] = []
    if isinstance(actions_raw, list):
        for raw in actions_raw:
            item = _as_dict(raw)
            actions.append({to_str(k): to_optional_str(v) for k, v in item.items()})
    return ReviewCaseDetail(
        review_case_id=to_str(rc.get("review_case_id")),
        queue_state=to_str(rc.get("queue_state")),
        priority=to_int(rc.get("priority")),
        assigned_to=to_optional_str(rc.get("assigned_to")),
        follow_up_at=to_iso_or_none(rc.get("follow_up_at")),
        sla_due_at=to_iso_or_none(rc.get("sla_due_at")),
        resolution=to_optional_str(rc.get("resolution")),
        resolved_at=to_iso_or_none(rc.get("resolved_at")),
        actions=actions,
        match_decision=map_match_decision(record),
        comparison_left=_map_comparison_entity(
            record.get("left_kind"), record.get("left_entity"), record.get("left_address")
        ),
        comparison_right=_map_comparison_entity(
            record.get("right_kind"), record.get("right_entity"), record.get("right_address")
        ),
        created_at=to_iso_or_empty(rc.get("created_at")),
        updated_at=to_iso_or_empty(rc.get("updated_at")),
    )


def _sanitize_properties(raw: GraphValue) -> dict[str, str | int | float | bool | None]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str | int | float | bool | None] = {}
    for key, val in raw.items():
        if isinstance(val, Neo4jDateTime):
            out[key] = val.to_native().isoformat()
        elif isinstance(val, datetime):
            out[key] = val.isoformat()
        elif isinstance(val, bool | int | float | str) or val is None:
            out[key] = val
        else:
            out[key] = str(val)
    return out


def _map_graph_nodes(raw_nodes: GraphValue) -> list[GraphNode]:
    if not isinstance(raw_nodes, list):
        return []
    return [
        GraphNode(
            id=to_str(n.get("id")),
            label=to_str(n.get("label")),
            properties=_sanitize_properties(n.get("properties")),
        )
        for item in raw_nodes
        if (n := _as_dict(item)) is not None
    ]


def _map_graph_edges(raw_edges: GraphValue) -> list[GraphEdge]:
    if not isinstance(raw_edges, list):
        return []
    return [
        GraphEdge(
            id=to_str(e.get("id")),
            source=to_str(e.get("source")),
            target=to_str(e.get("target")),
            type=to_str(e.get("type")),
            properties=_sanitize_properties(e.get("properties")),
        )
        for item in raw_edges
        if (e := _as_dict(item)) is not None
    ]


def map_person_graph(record: GraphRecord) -> PersonGraph:
    return PersonGraph(
        nodes=_map_graph_nodes(record.get("nodes")),
        edges=_map_graph_edges(record.get("edges")),
    )


def map_downstream_event(record: GraphRecord) -> DownstreamEvent:
    metadata_raw = record.get("metadata")
    metadata: dict[str, str] = {}
    if isinstance(metadata_raw, dict):
        metadata = {to_str(k): to_str(v) for k, v in metadata_raw.items()}
    return DownstreamEvent(
        event_id=to_str(record.get("event_id")),
        event_type=to_str(record.get("event_type")),
        affected_person_ids=to_str_list(record.get("affected_person_ids")),
        metadata=metadata,
        created_at=to_str(record.get("created_at")),
    )
