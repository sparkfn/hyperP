"""Map raw Neo4j records to Pydantic domain models."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal

from neo4j.time import DateTime as Neo4jDateTime
from pydantic.types import JsonValue

from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_float,
    to_int,
    to_iso_or_empty,
    to_iso_or_none,
    to_optional_float,
    to_optional_str,
    to_str,
    to_str_dict,
    to_str_list,
)
from src.types import (
    AddressSummary,
    AuditEvent,
    BankruptcyCase,
    ConnectionSource,
    DownstreamEvent,
    GraphEdge,
    GraphNode,
    KnowsRelationship,
    MatchDecision,
    MatchDecisionSummary,
    Person,
    PersonComparisonEntity,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    PersonSharedIdentifierCandidate,
    PersonStatus,
    PersonTimelineGroup,
    PossibleMatchDetail,
    ReviewCaseDetail,
    ReviewCaseSummary,
    SharedAddress,
    SharedIdentifier,
    SharedIdentifierGroup,
    SourceRecord,
    TimelineFact,
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
        lifetime_value=to_optional_float(record.get("lifetime_value")),
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
        entity_key=to_optional_str(record.get("entity_key")),
        entity_display_name=to_optional_str(record.get("entity_display_name")),
        record_type="conversation" if to_str(sr.get("record_type")) == "conversation" else "system",
        extraction_confidence=(
            to_float(sr.get("extraction_confidence"))
            if sr.get("extraction_confidence") is not None
            else None
        ),
        extraction_method=to_optional_str(sr.get("extraction_method")),
        link_status=to_str(sr.get("link_status")),
        linked_person_id=to_optional_str(record.get("linked_person_id")),
        observed_at=to_iso_or_empty(sr.get("observed_at")),
        ingested_at=to_iso_or_empty(sr.get("ingested_at")),
        conversation_ref=_parse_normalized_payload(sr.get("conversation_ref")) or None,
        raw_payload=_parse_normalized_payload(sr.get("raw_payload")) or None,
        normalized_payload=_parse_normalized_payload(sr.get("normalized_payload")),
    )


def _labelize(value: str) -> str:
    label = " ".join(part for part in value.split("_") if part)
    return label.capitalize() if label else "Unknown"


def _append_summary_fact(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    summary = payload.get("summary")
    if isinstance(summary, str) and summary.strip():
        facts.append(
            TimelineFact(
                fact_id="summary",
                category="source",
                label="Summary",
                value=summary.strip(),
            )
        )


def _append_attribute_facts(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    raw_attributes = payload.get("attributes")
    if not isinstance(raw_attributes, list):
        return
    for index, raw in enumerate(raw_attributes):
        item = _json_dict(raw)
        name = _json_str(item.get("attribute_name"))
        value = _json_str(item.get("attribute_value"))
        if name is None or value is None or value == "":
            continue
        category: Literal["identity", "source"] = (
            "identity" if name in {"full_name", "dob"} else "source"
        )
        facts.append(
            TimelineFact(
                fact_id=f"attribute-{index}",
                category=category,
                label=_labelize(name),
                value=value,
                detail=_json_str(item.get("quality_flag")),
            )
        )


def _append_identifier_facts(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    raw_identifiers = payload.get("identifiers")
    if not isinstance(raw_identifiers, list):
        return
    for index, raw in enumerate(raw_identifiers):
        item = _json_dict(raw)
        identifier_type = _json_str(item.get("identifier_type"))
        normalized_value = _json_str(item.get("normalized_value"))
        if identifier_type is None or normalized_value is None or normalized_value == "":
            continue
        category: Literal["contact", "identity"] = (
            "contact" if identifier_type in {"phone", "email"} else "identity"
        )
        facts.append(
            TimelineFact(
                fact_id=f"identifier-{index}",
                category=category,
                label=_labelize(identifier_type),
                value=normalized_value,
                detail=_json_str(item.get("quality_flag")),
            )
        )


def _append_address_fact(facts: list[TimelineFact], payload: dict[str, JsonValue]) -> None:
    address = _json_dict(payload.get("address"))
    normalized = _json_str(address.get("normalized_full"))
    if normalized is None or normalized == "":
        return
    facts.append(
        TimelineFact(
            fact_id="address",
            category="address",
            label="Address",
            value=normalized,
            detail=_json_str(address.get("quality_flag")),
        )
    )


def map_bankruptcy_case(record: GraphRecord) -> BankruptcyCase:
    bc = _as_dict(record.get("bankruptcy_case"))
    return BankruptcyCase(
        bankruptcy_case_id=to_str(bc.get("bankruptcy_case_id")),
        source_system_key=to_str(bc.get("source_system_key")),
        source_case_id=to_str(bc.get("source_case_id")),
        case_number=to_optional_str(bc.get("case_number")),
        document_type=to_optional_str(bc.get("document_type")),
        document_date=to_iso_or_none(bc.get("document_date"))
        or to_optional_str(bc.get("document_date")),
        event_type=to_optional_str(bc.get("event_type")),
        event_date=to_iso_or_none(bc.get("event_date")) or to_optional_str(bc.get("event_date")),
        trustee_name=to_optional_str(bc.get("trustee_name")),
        trustee_firm=to_optional_str(bc.get("trustee_firm")),
        source_url=to_optional_str(bc.get("source_url")),
        first_seen_at=to_iso_or_none(bc.get("first_seen_at")),
        last_seen_at=to_iso_or_none(bc.get("last_seen_at")),
        created_at=to_iso_or_none(bc.get("created_at")),
        updated_at=to_iso_or_none(bc.get("updated_at")),
    )


def _append_bankruptcy_case_facts(facts: list[TimelineFact], value: GraphValue) -> None:
    case = _as_dict(value)
    case_number = to_optional_str(case.get("case_number")) or to_optional_str(
        case.get("source_case_id")
    )
    if case_number is not None:
        facts.append(
            TimelineFact(
                fact_id="bankruptcy_case",
                category="bankruptcy",
                label="Bankruptcy case",
                value=case_number,
            )
        )
    event_type = to_optional_str(case.get("event_type"))
    event_date = to_iso_or_none(case.get("event_date")) or to_optional_str(case.get("event_date"))
    if event_type is not None:
        facts.append(
            TimelineFact(
                fact_id="bankruptcy_event",
                category="bankruptcy",
                label="Bankruptcy event",
                value=event_type,
                detail=event_date,
            )
        )
    document_type = to_optional_str(case.get("document_type"))
    document_date = to_iso_or_none(case.get("document_date")) or to_optional_str(
        case.get("document_date")
    )
    if document_type is not None:
        facts.append(
            TimelineFact(
                fact_id="bankruptcy_document",
                category="bankruptcy",
                label="Bankruptcy document",
                value=document_type,
                detail=document_date,
            )
        )
    trustee_name = to_optional_str(case.get("trustee_name"))
    trustee_firm = to_optional_str(case.get("trustee_firm"))
    if trustee_name is not None:
        facts.append(
            TimelineFact(
                fact_id="bankruptcy_trustee",
                category="bankruptcy",
                label="Trustee",
                value=trustee_name,
                detail=trustee_firm,
            )
        )


def _timeline_facts(payload: dict[str, JsonValue] | None) -> list[TimelineFact]:
    if payload is None:
        return []
    facts: list[TimelineFact] = []
    _append_summary_fact(facts, payload)
    _append_attribute_facts(facts, payload)
    _append_identifier_facts(facts, payload)
    _append_address_fact(facts, payload)
    return facts


def map_timeline_group(record: GraphRecord) -> PersonTimelineGroup:
    sr = _as_dict(record.get("source_record"))
    observed_at = to_iso_or_none(sr.get("observed_at"))
    ingested_at = to_iso_or_empty(sr.get("ingested_at"))
    occurred_at = observed_at if observed_at is not None else ingested_at
    facts = _timeline_facts(_parse_normalized_payload(sr.get("normalized_payload")))
    _append_bankruptcy_case_facts(facts, record.get("bankruptcy_case"))
    return PersonTimelineGroup(
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
        occurred_at=occurred_at,
        timestamp_kind="source" if observed_at is not None else "fallback",
        ingested_at=ingested_at,
        facts=facts,
    )


def map_person_entity_dict(raw: GraphValue) -> PersonEntitySummary:
    entity = _as_dict(raw)
    return PersonEntitySummary(
        entity_key=to_str(entity.get("entity_key")),
        display_name=to_optional_str(entity.get("display_name")),
        entity_type=to_optional_str(entity.get("entity_type")),
        country_code=to_optional_str(entity.get("country_code")),
        is_active=bool(entity.get("is_active", True)),
        source_record_count=to_int(entity.get("source_record_count")),
    )


def _map_source_records(value: GraphValue) -> list[SourceRecord]:
    if not isinstance(value, list):
        return []
    records: list[SourceRecord] = []
    for raw in value:
        record = _as_dict(raw)
        if "source_record" in record:
            records.append(map_source_record(record))
        elif "source_record_pk" in record:
            records.append(map_source_record({"source_record": record, **record}))
    return records


def map_person_identifier(record: GraphRecord) -> PersonIdentifier:
    raw_entities = record.get("entities")
    entities = (
        [map_person_entity_dict(raw) for raw in raw_entities]
        if isinstance(raw_entities, list)
        else []
    )
    return PersonIdentifier(
        identifier_type=to_str(record.get("identifier_type")),
        normalized_value=to_str(record.get("normalized_value")),
        is_active=bool(record.get("is_active", True)),
        is_verified=bool(record.get("is_verified", False)),
        last_confirmed_at=to_iso_or_none(record.get("last_confirmed_at")),
        source_system_key=to_optional_str(record.get("source_system_key")),
        source_record_pks=to_str_list(record.get("source_record_pks")),
        source_record_ids=to_str_list(record.get("source_record_ids")),
        entities=entities,
        source_records=_map_source_records(record.get("source_records")),
    )


def _map_shared_identifiers(value: GraphValue) -> list[SharedIdentifier]:
    if not isinstance(value, list):
        return []
    return [
        SharedIdentifier(
            identifier_type=to_str(d.get("identifier_type")),
            normalized_value=to_str(d.get("normalized_value")),
        )
        for raw in value
        if (d := _as_dict(raw)).get("identifier_type")
    ]


def _identifier_strength(identifiers: list[SharedIdentifier]) -> Literal["strong", "weak"]:
    if any(identifier.identifier_type == "nric" for identifier in identifiers):
        return "strong"
    return "weak"


def map_shared_identifier_candidate(record: GraphRecord) -> PersonSharedIdentifierCandidate:
    identifiers = _map_shared_identifiers(record.get("identifiers"))
    return PersonSharedIdentifierCandidate(
        person_id=to_str(record.get("person_id")),
        status=to_str(record.get("status")),
        preferred_full_name=to_optional_str(record.get("preferred_full_name")),
        preferred_phone=to_optional_str(record.get("preferred_phone")),
        preferred_email=to_optional_str(record.get("preferred_email")),
        preferred_dob=to_optional_str(record.get("preferred_dob")),
        profile_completeness_score=to_float(record.get("profile_completeness_score")),
        identifier_strength=_identifier_strength(identifiers),
        identifiers=identifiers,
    )


def _map_possible_match_source_records(value: GraphValue) -> list[SourceRecord]:
    if not isinstance(value, list):
        return []
    return [map_source_record({"source_record": _as_dict(raw), **_as_dict(raw)}) for raw in value]


def map_possible_match_detail(records: list[GraphRecord]) -> PossibleMatchDetail:
    first = records[0]
    return PossibleMatchDetail(
        candidate_person_id=to_str(first.get("candidate_person_id")),
        candidate_name=to_optional_str(first.get("candidate_name")),
        shared_identifier_groups=[
            SharedIdentifierGroup(
                identifier_type=to_str(record.get("identifier_type")),
                normalized_value=to_str(record.get("normalized_value")),
                candidate_source_records=_map_possible_match_source_records(
                    record.get("candidate_source_records")
                ),
                current_person_source_records=_map_possible_match_source_records(
                    record.get("current_person_source_records")
                ),
            )
            for record in records
        ],
    )


def _map_shared_addresses(value: GraphValue) -> list[SharedAddress]:
    if not isinstance(value, list):
        return []
    return [
        SharedAddress(
            address_id=to_str(d.get("address_id")),
            normalized_full=to_optional_str(d.get("normalized_full")),
            source_system_key=to_optional_str(d.get("source_system_key")),
        )
        for raw in value
        if (d := _as_dict(raw)).get("address_id")
    ]


def _map_knows_relationships(value: GraphValue) -> list[KnowsRelationship]:
    if not isinstance(value, list):
        return []
    return [
        KnowsRelationship(
            relationship_label=to_optional_str(d.get("relationship_label")),
            relationship_category=to_str(d.get("relationship_category")),
            source_system_key=to_optional_str(d.get("source_system_key")),
        )
        for raw in value
        if (d := _as_dict(raw)).get("relationship_category")
    ]


def _map_connection_sources(value: GraphValue) -> list[ConnectionSource]:
    if not isinstance(value, list):
        return []
    return [
        ConnectionSource(
            source_system_key=to_str(d.get("source_system_key")),
            entity_display_name=to_optional_str(d.get("entity_display_name")),
        )
        for raw in value
        if (d := _as_dict(raw)).get("source_system_key")
    ]


def map_connection(record: GraphRecord) -> PersonConnection:
    return PersonConnection(
        person_id=to_str(record.get("person_id")),
        status=to_str(record.get("status")),
        preferred_full_name=to_optional_str(record.get("preferred_full_name")),
        hops=to_int(record.get("hops")),
        shared_identifiers=_map_shared_identifiers(record.get("shared_identifiers")),
        shared_addresses=_map_shared_addresses(record.get("shared_addresses")),
        knows_relationships=_map_knows_relationships(record.get("knows_relationships")),
        connection_sources=_map_connection_sources(record.get("connection_sources")),
    )


def map_audit_event(record: GraphRecord) -> AuditEvent:
    me = _as_dict(record.get("merge_event"))
    metadata = to_str_dict(me.get("metadata"))
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


def _json_payload_from_mapping(value: dict[str, GraphValue]) -> dict[str, JsonValue]:
    payload: dict[str, JsonValue] = {}
    for key, item in value.items():
        if isinstance(key, str) and _is_json_value(item):
            payload[key] = _to_json_value(item)
    return payload


def _to_json_value(value: object) -> JsonValue:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        return [_to_json_value(item) for item in value if _is_json_value(item)]
    if isinstance(value, dict):
        return {
            key: _to_json_value(item)
            for key, item in value.items()
            if isinstance(key, str) and _is_json_value(item)
        }
    return None


def _parse_normalized_payload(value: GraphValue) -> dict[str, JsonValue]:
    if isinstance(value, dict):
        return _json_payload_from_mapping(value)
    if not isinstance(value, str):
        return {}
    try:
        parsed: object = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    payload: dict[str, JsonValue] = {}
    for key, item in parsed.items():
        if isinstance(key, str) and _is_json_value(item):
            payload[key] = _to_json_value(item)
    return payload


def _is_json_value(value: object) -> bool:
    if isinstance(value, str | int | float | bool) or value is None:
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _json_dict(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def _json_str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _attribute_value(
    payload: dict[str, JsonValue], name: Literal["full_name", "dob"]
) -> str | None:
    attrs = payload.get("attributes")
    if not isinstance(attrs, list):
        return None
    for raw in attrs:
        item = _json_dict(raw)
        if item.get("attribute_name") == name:
            return _json_str(item.get("attribute_value"))
    return None


def _identifier_value(
    payload: dict[str, JsonValue], identifier_type: Literal["phone", "email"]
) -> str | None:
    ids = payload.get("identifiers")
    if not isinstance(ids, list):
        return None
    for raw in ids:
        item = _json_dict(raw)
        if item.get("identifier_type") == identifier_type:
            return _json_str(item.get("normalized_value"))
    return None


def _source_record_address(payload: dict[str, JsonValue]) -> AddressSummary | None:
    addr = _json_dict(payload.get("address"))
    if not addr:
        return None
    normalized = _json_str(addr.get("normalized_full"))
    if normalized is None:
        return None
    return AddressSummary(
        address_id="",
        unit_number=_json_str(addr.get("unit_number")),
        street_number=_json_str(addr.get("street_number")),
        street_name=_json_str(addr.get("street_name")),
        city=_json_str(addr.get("city")),
        postal_code=_json_str(addr.get("postal_code")),
        country_code=_json_str(addr.get("country_code")),
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
    return DownstreamEvent(
        event_id=to_str(record.get("event_id")),
        event_type=to_str(record.get("event_type")),
        affected_person_ids=to_str_list(record.get("affected_person_ids")),
        metadata=to_str_dict(record.get("metadata")),
        created_at=to_str(record.get("created_at")),
    )
