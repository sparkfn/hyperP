"""Map raw Neo4j records to Pydantic domain models."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Literal, cast, get_args

from neo4j.time import DateTime as Neo4jDateTime
from pydantic.types import JsonValue

from src.display_format import format_display_date
from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_float,
    to_int,
    to_iso_or_empty,
    to_iso_or_none,
    to_optional_bool,
    to_optional_float,
    to_optional_int,
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
    LoyaltySummary,
    MatchDecision,
    MatchDecisionSummary,
    NonVehicleLine,
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
    SalesOrderSummary,
    SalesVehicleSummary,
    SharedAddress,
    SharedIdentifier,
    SharedIdentifierGroup,
    SourceRecord,
    SourceRecordEntityFacet,
    SourceRecordLifecycleStatus,
    SourceRecordTypeLiteral,
    TimelineFact,
    VehicleSummary,
)

_RECORD_TYPES: frozenset[str] = frozenset(get_args(SourceRecordTypeLiteral))
_LIFECYCLE_STATUSES: frozenset[str] = frozenset(get_args(SourceRecordLifecycleStatus))


def _to_history_family(value: GraphValue) -> Literal["activity", "stage"] | None:
    """Return a known history family; unknown values remain non-disclosive."""
    raw = to_optional_str(value)
    if raw in {"activity", "stage"}:
        return cast("Literal['activity', 'stage']", raw)
    return None


def _to_record_type(value: GraphValue) -> SourceRecordTypeLiteral:
    """Coerce a stored ``record_type`` to the current literal.

    Legacy ``'system'`` rows (pre-backfill) and any unknown value fall back to
    ``'identity'`` — the behaviour-preserving default for the system family.
    """
    raw = to_str(value)
    if raw in _RECORD_TYPES:
        return cast("SourceRecordTypeLiteral", raw)
    return "identity"


def _to_lifecycle_status(value: GraphValue) -> SourceRecordLifecycleStatus:
    if value is None:
        return "active"
    raw = to_str(value)
    if raw not in _LIFECYCLE_STATUSES:
        raise ValueError(f"unexpected SourceRecord lifecycle_status: {raw!r}")
    return cast("SourceRecordLifecycleStatus", raw)


def _as_dict(value: GraphValue) -> GraphRecord:
    """Coerce a graph value to a dict, returning empty dict for non-dicts/None."""
    if isinstance(value, dict):
        return value
    return {}


def _map_loyalty(rows: GraphValue) -> list[LoyaltySummary]:
    """Read-through loyalty balances: one entry per source system, latest observed wins."""
    if not isinstance(rows, list):
        return []

    def _obs_key(row: GraphRecord) -> tuple[str, str]:
        # Latest observed_at wins; tiebreak on source_record_pk so the choice is
        # deterministic across fetches (no flapping) rather than traversal-order.
        return (
            to_iso_or_none(row.get("observed_at")) or "",
            to_str(row.get("source_record_pk")) or "",
        )

    ordered = sorted(
        (r for r in rows if isinstance(r, dict)),
        key=_obs_key,
        reverse=True,
    )
    seen: set[str] = set()
    out: list[LoyaltySummary] = []
    for row in ordered:
        src = to_str(row.get("source_system")) or ""
        if not src or src in seen:
            continue
        raw = row.get("raw_payload")
        try:
            raw_dict = json.loads(raw) if isinstance(raw, str) else None
        except (TypeError, ValueError):
            raw_dict = None
        if not isinstance(raw_dict, dict):
            continue
        block = raw_dict.get("loyalty")
        if not isinstance(block, dict):
            continue
        seen.add(src)
        out.append(
            LoyaltySummary(
                source_system=src,
                points=to_optional_int(block.get("points")),
                disable_loyalty=to_optional_bool(block.get("disable_loyalty")),
                current_spend_for_points=to_optional_float(block.get("current_spend_for_points")),
                current_sales_for_discount=to_optional_float(
                    block.get("current_sales_for_discount")
                ),
                observed_at=to_iso_or_none(row.get("observed_at")),
            )
        )
    return out


def _map_vehicles(rows: GraphValue) -> list[VehicleSummary]:
    """Map Vehicle + OWNS_VEHICLE/BOUGHT_VEHICLE edges to VehicleSummary.

    Dedups by vehicle_id — a person can have multiple edges to one vehicle
    (e.g. two OWNS_VEHICLE rels MERGEd on distinct source_order_id). When a
    vehicle has both OWNS and BOUGHT edges, OWNS wins (the stronger ownership claim).
    """
    if not isinstance(rows, list):
        return []

    def _owns_first(row: GraphRecord) -> int:
        return 0 if to_str(row.get("rel_type")) == "OWNS_VEHICLE" else 1

    ordered = sorted((r for r in rows if isinstance(r, dict)), key=_owns_first)
    seen: set[str] = set()
    out: list[VehicleSummary] = []
    for row in ordered:
        vehicle_id = row.get("vehicle_id")
        if vehicle_id is None:
            continue
        key = to_str(vehicle_id)
        if not key or key in seen:
            continue
        seen.add(key)
        rel_type = to_str(row.get("rel_type")) or ""
        relationship: Literal["OWNS", "BOUGHT"] = "OWNS" if rel_type == "OWNS_VEHICLE" else "BOUGHT"
        out.append(
            VehicleSummary(
                vehicle_id=key,
                product=to_optional_str(row.get("product")),
                product_sku=to_optional_str(row.get("product_sku")),
                manufacturer=to_optional_str(row.get("manufacturer")),
                model=to_optional_str(row.get("model")),
                lta_tag=to_optional_str(row.get("lta_tag")),
                serial_number=to_optional_str(row.get("serial_number")),
                relationship=relationship,
                is_active=to_optional_bool(row.get("is_active")),
                conflict_flag=to_optional_bool(row.get("conflict_flag")),
                observed_at=to_iso_or_none(row.get("observed_at")),
            )
        )
    return out


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
    loyalty = _map_loyalty(record.get("loyalty_rows"))
    vehicles = _map_vehicles(record.get("vehicles"))
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
        preferred_race_ethnicity=to_optional_str(p.get("preferred_race_ethnicity")),
        profile_completeness_score=to_float(p.get("profile_completeness_score")),
        golden_profile_computed_at=to_iso_or_none(p.get("golden_profile_computed_at")),
        golden_profile_version=to_optional_str(p.get("golden_profile_version")),
        source_record_count=to_int(record.get("source_record_count")),
        connection_count=to_int(record.get("connection_count")),
        lifetime_value=to_optional_float(record.get("lifetime_value")),
        loyalty=loyalty or None,
        vehicles=vehicles or None,
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
        record_type=_to_record_type(sr.get("record_type")),
        lifecycle_status=_to_lifecycle_status(sr.get("lifecycle_status")),
        extraction_confidence=(
            to_float(sr.get("extraction_confidence"))
            if sr.get("extraction_confidence") is not None
            else None
        ),
        extraction_method=to_optional_str(sr.get("extraction_method")),
        link_status=to_str(sr.get("link_status")),
        linked_person_id=to_optional_str(record.get("linked_person_id")),
        parent_source_system=to_optional_str(sr.get("parent_source_system")),
        parent_source_record_id=to_optional_str(sr.get("parent_source_record_id")),
        parent_record_type=(
            _to_record_type(sr.get("parent_record_type"))
            if sr.get("parent_record_type") is not None
            else None
        ),
        history_family=_to_history_family(sr.get("history_family")),
        history_kind=to_optional_str(sr.get("history_kind")),
        history_source=to_optional_str(sr.get("history_source")),
        event_category_id=to_optional_str(sr.get("event_category_id")),
        event_stage_id=to_optional_str(sr.get("event_stage_id")),
        event_stage_semantic_id=to_optional_str(sr.get("event_stage_semantic_id")),
        event_at=to_iso_or_none(sr.get("event_at")),
        history_projection_version=to_optional_str(sr.get("history_projection_version")),
        history_projection_source=to_optional_str(sr.get("history_projection_source")),
        history_projected_at=to_iso_or_none(sr.get("history_projected_at")),
        observed_at=to_iso_or_empty(sr.get("observed_at")),
        ingested_at=to_iso_or_empty(sr.get("ingested_at")),
        conversation_ref=_parse_normalized_payload(sr.get("conversation_ref")) or None,
        raw_payload=_parse_normalized_payload(sr.get("raw_payload")) or None,
        normalized_payload=_parse_normalized_payload(sr.get("normalized_payload")),
    )


def map_source_record_entity_facet(record: GraphRecord) -> SourceRecordEntityFacet:
    return SourceRecordEntityFacet(
        source_system=to_str(record.get("source_system")),
        entity_key=to_optional_str(record.get("entity_key")),
        entity_display_name=to_optional_str(record.get("entity_display_name")),
        count=to_int(record.get("count")),
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
        record_type=_to_record_type(sr.get("record_type")),
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
        preferred_dob=_fmt_dob(to_optional_str(record.get("preferred_dob"))),
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
        review_candidate_person_ids=to_str_list(md.get("review_candidate_person_ids")),
        created_at=to_iso_or_empty(md.get("created_at")),
        left_person_id=to_optional_str(record.get("left_person_id")),
        right_person_id=to_optional_str(record.get("right_person_id")),
        review_case_id=to_optional_str(record.get("review_case_id")),
        review_case_queue_state=to_optional_str(record.get("review_case_queue_state")),
        review_case_assigned_to=to_optional_str(record.get("review_case_assigned_to")),
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
        resolution=to_optional_str(rc.get("resolution")),
        resolved_at=to_iso_or_none(rc.get("resolved_at")),
        left_person_id=to_optional_str(record.get("left_person_id")),
        left_person_name=to_optional_str(record.get("left_person_name")),
        left_person_status=to_optional_str(record.get("left_person_status")),
        right_person_id=to_optional_str(record.get("right_person_id")),
        right_person_name=to_optional_str(record.get("right_person_name")),
        right_person_status=to_optional_str(record.get("right_person_status")),
        match_decision=MatchDecisionSummary(
            match_decision_id=to_str(md.get("match_decision_id")),
            engine_type=to_str(md.get("engine_type")),
            decision=to_str(md.get("decision")),
            confidence=to_float(md.get("confidence")),
        ),
    )


def _map_sales_summary(
    sales_order: GraphValue,
    sales_vehicles: GraphValue,
    non_vehicle_lines: GraphValue = None,
) -> SalesOrderSummary | None:
    order = _as_dict(sales_order)
    if not order:
        return None
    vehicles: list[SalesVehicleSummary] = []
    if isinstance(sales_vehicles, list):
        for raw in sales_vehicles:
            u = _as_dict(raw)
            if not u:
                continue
            vehicles.append(
                SalesVehicleSummary(
                    vehicle_id=to_str(u.get("vehicle_id")),
                    product=to_optional_str(u.get("product")),
                    product_sku=to_optional_str(u.get("product_sku")),
                    normalized_lta_tag=to_optional_str(u.get("normalized_lta_tag")),
                    normalized_serial_number=to_optional_str(u.get("normalized_serial_number")),
                    conflict_flag=bool(u.get("conflict_flag", False)),
                )
            )
    parsed_non_vehicle_lines: list[NonVehicleLine] = _parse_non_vehicle_lines(non_vehicle_lines)
    return SalesOrderSummary(
        order_id=to_str(order.get("order_id")),
        order_no=to_optional_str(order.get("order_no")),
        total_amount=to_optional_float(order.get("total_amount")),
        currency=to_optional_str(order.get("currency")),
        ordered_at=to_optional_str(order.get("ordered_at")),
        vehicles=vehicles,
        non_vehicle_lines=parsed_non_vehicle_lines,
    )


def _fmt_dob(value: str | None) -> str | None:
    if value is None:
        return None
    formatted = format_display_date(value)
    return formatted if formatted else value


def _parse_non_vehicle_lines(value: GraphValue) -> list[NonVehicleLine]:
    """Parse ``Order.non_vehicle_lines`` from Cypher into ``NonVehicleLine`` list.

    Defensive parsing — Neo4j stores the list as a JSON string (cannot store
    LIST<MAP>), but callers may also pass an already-parsed list. Accepts:
    ``None``, ``""``, valid JSON string, invalid JSON string, list.
    """
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, str):
        if not value:
            return []
        try:
            parsed: object = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        raw_items = parsed
    else:
        return []

    out: list[NonVehicleLine] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        raw_dict = cast(dict[str, JsonValue], _to_json_value(item) or {})
        if not isinstance(raw_dict, dict):
            raw_dict = {}
        out.append(
            NonVehicleLine(
                product_sku=to_optional_str(item.get("sku")),
                product=to_optional_str(item.get("product_name")),
                merchant=to_optional_str(item.get("merchant")),
                manufacturer=to_optional_str(item.get("manufacturer")),
                serial_number=to_optional_str(item.get("serial_number")),
                quantity=to_optional_float(item.get("quantity")),
                unit_price=to_optional_float(item.get("unit_price")),
                total_amount=to_optional_float(item.get("line_total")),
                currency=to_optional_str(item.get("currency")),
                category=to_optional_str(item.get("category")),
                raw=raw_dict,
            )
        )
    return out


def _map_comparison_entity(
    kind: GraphValue,
    entity: GraphValue,
    address: GraphValue,
    sales_order: GraphValue = None,
    sales_vehicles: GraphValue = None,
    non_vehicle_lines: GraphValue = None,
) -> PersonComparisonEntity | None:
    e = _as_dict(entity)
    if not e:
        return None
    kind_str = to_optional_str(kind)
    if kind_str == "source_record":
        return _map_source_record_comparison(
            e,
            sales_order=sales_order,
            sales_vehicles=sales_vehicles,
            non_vehicle_lines=non_vehicle_lines,
        )
    return PersonComparisonEntity(
        entity_kind="person",
        person_id=to_optional_str(e.get("person_id")),
        status=to_optional_str(e.get("status")),
        preferred_full_name=to_optional_str(e.get("preferred_full_name")),
        preferred_phone=to_optional_str(e.get("preferred_phone")),
        preferred_email=to_optional_str(e.get("preferred_email")),
        preferred_dob=_fmt_dob(to_optional_str(e.get("preferred_dob"))),
        preferred_address=map_address(address),
    )


def _map_source_record_comparison(
    e: GraphRecord,
    sales_order: GraphValue = None,
    sales_vehicles: GraphValue = None,
    non_vehicle_lines: GraphValue = None,
) -> PersonComparisonEntity:
    payload = _parse_normalized_payload(e.get("normalized_payload"))
    return PersonComparisonEntity(
        entity_kind="source_record",
        source_record_pk=to_optional_str(e.get("source_record_pk")),
        source_record_id=to_optional_str(e.get("source_record_id")),
        source_system_key=to_optional_str(e.get("source_system_key")),
        observed_at=to_iso_or_none(e.get("observed_at")),
        record_type=to_optional_str(e.get("record_type")),
        linked_person_id=to_optional_str(e.get("linked_person_id")),
        status=None,
        preferred_full_name=_attribute_value(payload, "full_name"),
        preferred_phone=_identifier_value(payload, "phone"),
        preferred_email=_identifier_value(payload, "email"),
        preferred_dob=_fmt_dob(_attribute_value(payload, "dob")),
        preferred_address=_source_record_address(payload),
        sales_summary=_map_sales_summary(sales_order, sales_vehicles, non_vehicle_lines),
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
        # Each entry is a JSON string (Neo4j cannot store maps as properties).
        for raw in actions_raw:
            if not isinstance(raw, str):
                continue
            try:
                loaded: object = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                actions.append({to_str(k): to_optional_str(v) for k, v in loaded.items()})
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
            record.get("left_kind"),
            record.get("left_entity"),
            record.get("left_address"),
            sales_order=record.get("sales_order"),
            sales_vehicles=record.get("sales_vehicles"),
            non_vehicle_lines=record.get("non_vehicle_lines"),
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
