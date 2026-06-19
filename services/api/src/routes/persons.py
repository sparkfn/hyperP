"""Person read endpoints: search, fetch, source records, connections, audit, matches."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from src.auth.deps import require_scope
from src.chat_transcript import parse_chat_transcript
from src.display_format import format_confidence_pct, format_display_datetime
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.repositories.deps import get_person_repo
from src.repositories.protocols.person import PersonListFilters, PersonRepository
from src.types import (
    ApiResponse,
    AuditEvent,
    BankruptcyCase,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    PersonSharedIdentifierCandidate,
    PersonTimelineGroup,
    PopoverDisplayItem,
    PossibleMatchDetail,
    SourceRecord,
    SourceRecordEntityFacet,
    SourceRecordView,
)

router = APIRouter(
    prefix="/v1/persons",
    tags=["Persons"],
    dependencies=[Depends(require_scope("persons:read"))],
)

_ALLOWED_SORT: frozenset[str] = frozenset(
    {
        "preferred_full_name",
        "preferred_phone",
        "preferred_email",
        "preferred_dob",
        "preferred_nric",
        "source_record_count",
        "connection_count",
        "entity_count",
        "possible_match_count",
        "system_match_count",
        "order_count",
        "bankruptcy_case_count",
        "phone_confidence",
        "updated_at",
        "profile_completeness_score",
        "relevance",
    }
)


def _identifier_display_items(items: list[PersonIdentifier]) -> list[PopoverDisplayItem]:
    return [
        PopoverDisplayItem(
            primary=f"{item.identifier_type}: {item.normalized_value}",
            secondary=" · ".join(
                filter(
                    None,
                    [
                        "active" if item.is_active else "inactive",
                        "verified" if item.is_verified else "unverified",
                        item.source_system_key or "",
                    ],
                )
            ),
        )
        for item in items
    ]


def _connection_display_items(items: list[PersonConnection]) -> list[PopoverDisplayItem]:
    return [
        PopoverDisplayItem(
            primary=item.preferred_full_name or item.person_id,
            secondary=" · ".join(
                [
                    f"{identifier.identifier_type}:{identifier.normalized_value}"
                    for identifier in item.shared_identifiers
                ]
                + [
                    f"address:{address.normalized_full or address.address_id}"
                    for address in item.shared_addresses
                ]
                + [
                    f"knows:{rel.relationship_label or rel.relationship_category}"
                    for rel in item.knows_relationships
                ]
            ),
        )
        for item in items
    ]


def _source_record_display_items(items: list[SourceRecord]) -> list[PopoverDisplayItem]:
    return [
        PopoverDisplayItem(
            primary=item.source_system,
            secondary=" · ".join([item.source_record_id, item.record_type, item.ingested_at]),
        )
        for item in items
    ]


def _to_source_record_view(item: SourceRecord) -> SourceRecordView:
    return SourceRecordView(
        **item.model_dump(),
        observed_at_display=format_display_datetime(item.observed_at),
        ingested_at_display=format_display_datetime(item.ingested_at),
        extraction_confidence_display=format_confidence_pct(item.extraction_confidence),
        chat_transcript=parse_chat_transcript(item.raw_payload),
    )


@router.get("", response_model=ApiResponse[list[ListedPerson]])
async def list_persons(
    request: Request,
    entity_key: list[str] | None = Query(default=None),
    entity_key_mode: str | None = Query(default=None),
    source_key: list[str] | None = Query(default=None),
    source_key_mode: str | None = Query(default=None),
    source_record_type: str | None = Query(default=None),
    is_high_value: bool | None = Query(default=None),
    is_high_risk: bool | None = Query(default=None),
    has_phone: bool | None = Query(default=None),
    has_email: bool | None = Query(default=None),
    has_any_contact: bool | None = Query(default=None),
    has_address: bool | None = Query(default=None),
    has_bankruptcy_case: bool | None = Query(default=None),
    has_any_match: bool | None = Query(default=None),
    has_possible_match: bool | None = Query(default=None),
    has_system_match: bool | None = Query(default=None),
    addr_street: str | None = Query(default=None),
    addr_unit: str | None = Query(default=None),
    addr_city: str | None = Query(default=None),
    addr_postal: str | None = Query(default=None),
    addr_country: str | None = Query(default=None),
    updated_after: str | None = Query(default=None),
    updated_before: str | None = Query(default=None),
    has_dob: bool | None = Query(default=None),
    dob_from: str | None = Query(default=None),
    dob_to: str | None = Query(default=None),
    dob_year: str | None = Query(default=None),
    dob_month: str | None = Query(default=None),
    dob_day: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort_by: str | None = Query(default=None),
    sort_order: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[ListedPerson]]:
    """Generalized person listing with multi-filter + single-column sort."""
    q_clean: str | None = q.strip() if q else None
    if q_clean is not None and len(q_clean) < 3:
        raise http_error(
            400,
            "invalid_request",
            "Search query q requires at least 3 characters (matches name, NRIC, email, phone).",
            request,
        )
    if sort_by is not None and sort_by not in _ALLOWED_SORT:
        raise http_error(400, "invalid_request", f"Unknown sort_by: {sort_by}", request)

    skip, page_limit = page_window(cursor, limit)
    filters: PersonListFilters = {
        "q": q_clean,
        "entity_keys": entity_key or None,
        "entity_key_mode": entity_key_mode,
        "source_keys": source_key or None,
        "source_key_mode": source_key_mode,
        "source_record_type": source_record_type,
        "is_high_value": is_high_value,
        "is_high_risk": is_high_risk,
        "has_phone": has_phone,
        "has_email": has_email,
        "has_any_contact": has_any_contact,
        "has_address": has_address,
        "has_bankruptcy_case": has_bankruptcy_case,
        "has_any_match": has_any_match,
        "has_possible_match": has_possible_match,
        "has_system_match": has_system_match,
        "addr_street": addr_street,
        "addr_unit": addr_unit,
        "addr_city": addr_city,
        "addr_postal": addr_postal,
        "addr_country": addr_country,
        "updated_after": updated_after,
        "updated_before": updated_before,
        "has_dob": has_dob,
        "dob_from": dob_from,
        "dob_to": dob_to,
        "dob_year": dob_year,
        "dob_month": dob_month,
        "dob_day": dob_day,
        "sort_by": sort_by,
        "sort_order": sort_order,
    }
    items, total = await repo.get_page(filters, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get("/search", response_model=ApiResponse[list[Person]])
async def search_persons(
    request: Request,
    identifier_type: str | None = Query(default=None),
    value: str | None = Query(default=None),
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[Person]]:
    """Operational person search by identifier or free-text."""
    if not identifier_type and not value and not q:
        raise http_error(400, "invalid_request", "Provide identifier_type+value or q.", request)

    skip, page_limit = page_window(cursor, limit)

    if identifier_type and value:
        persons = await repo.search_by_identifier(identifier_type, value)
        return envelope(persons, request)

    if q is None:
        return envelope([], request)
    if len(q) < 3:
        raise http_error(
            400,
            "invalid_request",
            "Search query q requires at least 3 characters (matches name, NRIC, email, phone).",
            request,
        )
    persons, has_more = await repo.search_by_query(q, status, skip, page_limit)
    return envelope(persons, request, next_cursor(skip, page_limit, has_more))


@router.get("/{person_id}", response_model=ApiResponse[Person])
async def get_person(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[Person]:
    """Return the canonical person view, resolving merge chain and address."""
    person = await repo.get_by_id(person_id)
    if person is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(person, request)


@router.get("/{person_id}/source-records", response_model=ApiResponse[list[SourceRecordView]])
async def get_person_source_records(
    person_id: str,
    request: Request,
    entity_key: str | None = Query(default=None),
    record_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[SourceRecordView]]:
    """List source records linked to a person (optionally filtered by entity/type)."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_source_records(
        person_id, skip, page_limit, entity_key=entity_key, record_type=record_type
    )
    views = [_to_source_record_view(item) for item in items]
    has_more = skip + page_limit < total
    resp = envelope(views, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = _source_record_display_items(items)
    return resp


@router.get(
    "/{person_id}/source-record-entities",
    response_model=ApiResponse[list[SourceRecordEntityFacet]],
)
async def get_person_source_record_entities(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[SourceRecordEntityFacet]]:
    """Per-entity source-record counts for a person (for filter chips)."""
    facets = await repo.get_source_record_entity_facets(person_id)
    return envelope(facets, request)


@router.get("/{person_id}/bankruptcy-cases", response_model=ApiResponse[list[BankruptcyCase]])
async def get_person_bankruptcy_cases(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[BankruptcyCase]]:
    """List bankruptcy cases linked to a person."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_bankruptcy_cases(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get("/{person_id}/timeline", response_model=ApiResponse[list[PersonTimelineGroup]])
async def get_person_timeline(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonTimelineGroup]]:
    """List source-record timeline groups for a person, newest source timestamp first."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_timeline(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get("/{person_id}/timeline/target", response_model=ApiResponse[PersonTimelineGroup])
async def get_person_timeline_target(
    person_id: str,
    request: Request,
    source_record_pk: str = Query(),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PersonTimelineGroup]:
    """Return one source-record timeline group for deep-link jumps."""
    item = await repo.get_timeline_target(person_id, source_record_pk)
    if item is None:
        raise http_error(404, "source_record_not_found", "Source record not found.", request)
    return envelope(item, request)


@router.get("/{person_id}/identifiers", response_model=ApiResponse[list[PersonIdentifier]])
async def get_person_identifiers(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonIdentifier]]:
    """Return all identifiers linked to a person, ordered by active status then type."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_identifiers(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    resp = envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = _identifier_display_items(items)
    return resp


@router.get("/{person_id}/connections", response_model=ApiResponse[list[PersonConnection]])
async def get_person_connections(
    person_id: str,
    request: Request,
    connection_type: ConnectionType = Query(default=ConnectionType.ALL),
    identifier_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonConnection]]:
    """Return persons connected through shared identifiers and/or addresses."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_connections(
        person_id, connection_type, identifier_type, skip, page_limit
    )
    has_more = skip + page_limit < total
    resp = envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
    resp.display_items = _connection_display_items(items)
    return resp


@router.get(
    "/{person_id}/shared-identifiers",
    response_model=ApiResponse[list[PersonSharedIdentifierCandidate]],
)
async def get_person_shared_identifiers(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonSharedIdentifierCandidate]]:
    """Return active persons sharing identifiers with a person."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_shared_identifier_candidates(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get(
    "/{person_id}/shared-identifiers/{candidate_id}/detail",
    response_model=ApiResponse[PossibleMatchDetail],
)
async def get_person_shared_identifier_detail(
    person_id: str,
    candidate_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PossibleMatchDetail]:
    """Return grouped source records for a shared-identifier possible match."""
    detail = await repo.get_possible_match_detail(person_id, candidate_id)
    if detail is None:
        raise http_error(
            404,
            "no_shared_identifiers",
            "No shared identifiers found between these persons.",
            request,
        )
    return envelope(detail, request)


@router.get("/{person_id}/entities", response_model=ApiResponse[list[PersonEntitySummary]])
async def get_person_entities(
    person_id: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonEntitySummary]]:
    """Return entities a person is linked to via source-record provenance."""
    items = await repo.get_entities(person_id)
    return envelope(items, request)


@router.get("/graph/node", response_model=ApiResponse[PersonGraph])
async def get_node_graph(
    request: Request,
    element_id: str = Query(),
    max_hops: int = Query(default=2, ge=1, le=4),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PersonGraph]:
    """Return the subgraph around any node identified by its element ID."""
    graph = await repo.get_node_graph(element_id, max_hops)
    if graph is None:
        raise http_error(404, "node_not_found", "Node not found.", request)
    return envelope(graph, request)


@router.get("/{person_id}/graph", response_model=ApiResponse[PersonGraph])
async def get_person_graph(
    person_id: str,
    request: Request,
    max_hops: int = Query(default=2, ge=1, le=4),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PersonGraph]:
    """Return the subgraph around a person up to *max_hops* hops away."""
    graph = await repo.get_graph(person_id, max_hops)
    if graph is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(graph, request)


@router.get("/{person_id}/relationships", response_model=ApiResponse[list[dict[str, str]]])
async def get_person_relationships(
    person_id: str, request: Request
) -> ApiResponse[list[dict[str, str]]]:
    """Post-MVP placeholder for typed person-to-person relationships."""
    _ = person_id
    return envelope([], request)


@router.get("/{person_id}/audit", response_model=ApiResponse[list[AuditEvent]])
async def get_person_audit(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[AuditEvent]]:
    """Return merge/unmerge audit events for a person."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_audit(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)


@router.get("/{person_id}/matches", response_model=ApiResponse[list[MatchDecision]])
async def get_person_matches(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[MatchDecision]]:
    """Return recent match decisions involving a person."""
    skip, page_limit = page_window(cursor, limit)
    items, total = await repo.get_matches(person_id, skip, page_limit)
    has_more = skip + page_limit < total
    return envelope(items, request, next_cursor(skip, page_limit, has_more), total_count=total)
