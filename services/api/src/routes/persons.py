"""Person read endpoints: search, fetch, source records, connections, audit, matches."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers import (
    map_audit_event,
    map_connection,
    map_match_decision,
    map_person,
    map_person_graph,
    map_source_record,
)
from src.graph.mappers_entities import map_listed_person, map_person_entity
from src.graph.mappers_sales import map_sales_order
from src.graph.queries import (
    DEFAULT_HOPS,
    FIND_PERSON_BY_IDENTIFIER,
    GET_PERSON_AUDIT,
    GET_PERSON_BY_ID,
    GET_PERSON_CONNECTIONS_ADDRESS,
    GET_PERSON_CONNECTIONS_ALL,
    GET_PERSON_CONNECTIONS_IDENTIFIER,
    GET_PERSON_CONNECTIONS_KNOWS,
    GET_PERSON_ENTITIES,
    GET_PERSON_MATCHES,
    GET_PERSON_SALES,
    GET_PERSON_SOURCE_RECORDS,
    MAX_HOPS,
    MIN_HOPS,
    SEARCH_PERSONS,
    build_count_persons_query,
    build_list_persons_query,
    get_graph_query,
    get_node_graph_query,
)
from src.http_utils import envelope, http_error, next_cursor, page_window
from src.types import (
    ApiResponse,
    AuditEvent,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    SalesOrder,
    SourceRecord,
)

router = APIRouter(prefix="/v1/persons")


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


def _connection_query(connection_type: ConnectionType) -> str:
    if connection_type is ConnectionType.IDENTIFIER:
        return GET_PERSON_CONNECTIONS_IDENTIFIER
    if connection_type is ConnectionType.ADDRESS:
        return GET_PERSON_CONNECTIONS_ADDRESS
    if connection_type is ConnectionType.KNOWS:
        return GET_PERSON_CONNECTIONS_KNOWS
    return GET_PERSON_CONNECTIONS_ALL


_ALLOWED_SORT: frozenset[str] = frozenset(
    {
        "preferred_full_name", "status", "preferred_phone", "preferred_email",
        "preferred_dob", "source_record_count", "connection_count",
        "phone_confidence", "updated_at", "profile_completeness_score", "relevance",
    }
)


@router.get("", response_model=ApiResponse[list[ListedPerson]])
async def list_persons(
    request: Request,
    status: str | None = Query(default=None),
    entity_key: str | None = Query(default=None),
    is_high_value: bool | None = Query(default=None),
    is_high_risk: bool | None = Query(default=None),
    has_phone: bool | None = Query(default=None),
    has_email: bool | None = Query(default=None),
    updated_after: str | None = Query(default=None),
    updated_before: str | None = Query(default=None),
    q: str | None = Query(default=None),
    sort_by: str | None = Query(default=None),
    sort_order: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[ListedPerson]]:
    """Generalized person listing with multi-filter + single-column sort."""
    q_clean: str | None = q.strip() if q else None
    if q_clean is not None and len(q_clean) < 3:
        raise http_error(
            400, "invalid_request", "Free-text query q requires at least 3 characters.", request
        )
    if sort_by is not None and sort_by not in _ALLOWED_SORT:
        raise http_error(400, "invalid_request", f"Unknown sort_by: {sort_by}", request)

    skip, page_limit = page_window(cursor, limit)
    has_q: bool = q_clean is not None
    list_query = build_list_persons_query(sort_by, sort_order, has_q=has_q)
    count_query = build_count_persons_query(has_q=has_q)
    list_params: dict[str, str | int | bool | None] = {
        "q": q_clean,
        "status": status,
        "entity_key": entity_key,
        "is_high_value": is_high_value,
        "is_high_risk": is_high_risk,
        "has_phone": has_phone,
        "has_email": has_email,
        "updated_after": updated_after,
        "updated_before": updated_before,
        "skip": skip,
        "limit": page_limit + 1,
    }
    count_params: dict[str, str | int | bool | None] = {
        k: v for k, v in list_params.items() if k not in ("skip", "limit")
    }
    async with get_session() as session:
        list_result = await session.run(list_query, list_params)
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in list_result]
        count_result = await session.run(count_query, count_params)
        count_record = await count_result.single()
    total: int = (
        int(count_record["total"])
        if count_record is not None and count_record["total"] is not None
        else 0
    )
    has_more = len(records) > page_limit
    items = [map_listed_person(rec) for rec in records[:page_limit]]
    return envelope(
        items, request, next_cursor(skip, page_limit, has_more), total_count=total
    )


@router.get("/search", response_model=ApiResponse[list[Person]])
async def search_persons(
    request: Request,
    identifier_type: str | None = Query(default=None),
    value: str | None = Query(default=None),
    q: str | None = Query(default=None),
    status: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[Person]]:
    """Operational person search by identifier or free-text."""
    if not identifier_type and not value and not q:
        raise http_error(400, "invalid_request", "Provide identifier_type+value or q.", request)

    skip, page_limit = page_window(cursor, limit)

    async with get_session() as session:
        if identifier_type and value:
            result = await session.run(
                FIND_PERSON_BY_IDENTIFIER, identifier_type=identifier_type, value=value
            )
            records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
            persons = [map_person(rec) for rec in records]
            return envelope(persons, request)

        if q is None:
            return envelope([], request)
        if len(q) < 3:
            raise http_error(
                400, "invalid_request", "Free-text query q requires at least 3 characters.", request
            )
        # Pass the fulltext query via a parameters dict to avoid colliding
        # with `session.run(query=...)`'s first positional argument name.
        result = await session.run(
            SEARCH_PERSONS,
            {"query": q, "status": status, "skip": skip, "limit": page_limit + 1},
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > page_limit
        persons = [map_person(rec) for rec in records[:page_limit]]
        return envelope(persons, request, next_cursor(skip, page_limit, has_more))


@router.get("/{person_id}", response_model=ApiResponse[Person])
async def get_person(person_id: str, request: Request) -> ApiResponse[Person]:
    """Return the canonical person view, resolving merge chain and address."""
    async with get_session() as session:
        result = await session.run(GET_PERSON_BY_ID, person_id=person_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(map_person(_record_to_dict(record.keys(), list(record.values()))), request)


@router.get("/{person_id}/source-records", response_model=ApiResponse[list[SourceRecord]])
async def get_person_source_records(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[SourceRecord]]:
    """List source records linked to a person."""
    skip, page_limit = page_window(cursor, limit)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_SOURCE_RECORDS, person_id=person_id, skip=skip, limit=page_limit + 1
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    items = [map_source_record(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))


@router.get("/{person_id}/connections", response_model=ApiResponse[list[PersonConnection]])
async def get_person_connections(
    person_id: str,
    request: Request,
    connection_type: ConnectionType = Query(default=ConnectionType.ALL),
    identifier_type: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[PersonConnection]]:
    """Return persons connected through shared identifiers and/or addresses."""
    skip, page_limit = page_window(cursor, limit)
    query = _connection_query(connection_type)
    async with get_session() as session:
        result = await session.run(
            query,
            person_id=person_id,
            identifier_type=identifier_type,
            skip=skip,
            limit=page_limit + 1,
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    items = [map_connection(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))


@router.get("/{person_id}/entities", response_model=ApiResponse[list[PersonEntitySummary]])
async def get_person_entities(
    person_id: str, request: Request
) -> ApiResponse[list[PersonEntitySummary]]:
    """Return entities a person is linked to via source-record provenance."""
    async with get_session() as session:
        result = await session.run(GET_PERSON_ENTITIES, person_id=person_id)
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    items = [map_person_entity(rec) for rec in records]
    return envelope(items, request)


@router.get("/{person_id}/sales", response_model=ApiResponse[list[SalesOrder]])
async def get_person_sales(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[SalesOrder]]:
    """Return sales orders for a person with line items and products."""
    skip, page_limit = page_window(cursor, limit)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_SALES, person_id=person_id, skip=skip, limit=page_limit + 1
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    items = [map_sales_order(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))


@router.get("/graph/node", response_model=ApiResponse[PersonGraph])
async def get_node_graph(
    request: Request,
    element_id: str = Query(),
    max_hops: int = Query(default=DEFAULT_HOPS, ge=MIN_HOPS, le=MAX_HOPS),
) -> ApiResponse[PersonGraph]:
    """Return the subgraph around any node identified by its Neo4j elementId."""
    query = get_node_graph_query(max_hops)
    async with get_session() as session:
        result = await session.run(query, element_id=element_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "node_not_found", "Node not found.", request)
    graph = map_person_graph(_record_to_dict(record.keys(), list(record.values())))
    return envelope(graph, request)


@router.get("/{person_id}/graph", response_model=ApiResponse[PersonGraph])
async def get_person_graph(
    person_id: str,
    request: Request,
    max_hops: int = Query(default=DEFAULT_HOPS, ge=MIN_HOPS, le=MAX_HOPS),
) -> ApiResponse[PersonGraph]:
    """Return the subgraph around a person up to *max_hops* hops away."""
    query = get_graph_query(max_hops)
    async with get_session() as session:
        result = await session.run(query, person_id=person_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    graph = map_person_graph(_record_to_dict(record.keys(), list(record.values())))
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
) -> ApiResponse[list[AuditEvent]]:
    """Return merge/unmerge audit events for a person."""
    skip, page_limit = page_window(cursor, limit)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_AUDIT, person_id=person_id, skip=skip, limit=page_limit + 1
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    items = [map_audit_event(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))


@router.get("/{person_id}/matches", response_model=ApiResponse[list[MatchDecision]])
async def get_person_matches(
    person_id: str,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None),
) -> ApiResponse[list[MatchDecision]]:
    """Return recent match decisions involving a person."""
    skip, page_limit = page_window(cursor, limit)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_MATCHES, person_id=person_id, skip=skip, limit=page_limit + 1
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    has_more = len(records) > page_limit
    items = [map_match_decision(rec) for rec in records[:page_limit]]
    return envelope(items, request, next_cursor(skip, page_limit, has_more))
