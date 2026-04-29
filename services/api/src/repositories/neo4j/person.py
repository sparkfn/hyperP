"""Neo4j implementation of PersonRepository."""

from __future__ import annotations

from src.graph.client import get_session
from src.graph.mappers import (
    map_audit_event,
    map_connection,
    map_match_decision,
    map_person,
    map_person_graph,
    map_person_identifier,
    map_source_record,
)
from src.graph.mappers_entities import map_listed_person, map_person_entity
from src.graph.queries import (
    COUNT_PERSON_AUDIT,
    COUNT_PERSON_CONNECTIONS_ADDRESS,
    COUNT_PERSON_CONNECTIONS_ALL,
    COUNT_PERSON_CONNECTIONS_IDENTIFIER,
    COUNT_PERSON_CONNECTIONS_KNOWS,
    COUNT_PERSON_IDENTIFIERS,
    COUNT_PERSON_SOURCE_RECORDS,
    FIND_PERSON_BY_IDENTIFIER,
    GET_PERSON_AUDIT,
    GET_PERSON_BY_ID,
    GET_PERSON_CONNECTIONS_ADDRESS,
    GET_PERSON_CONNECTIONS_ALL,
    GET_PERSON_CONNECTIONS_IDENTIFIER,
    GET_PERSON_CONNECTIONS_KNOWS,
    GET_PERSON_ENTITIES,
    GET_PERSON_IDENTIFIERS,
    GET_PERSON_MATCHES,
    GET_PERSON_SOURCE_RECORDS,
    SEARCH_PERSONS,
    build_count_persons_query,
    build_list_persons_query,
    get_graph_query,
    get_node_graph_query,
)
from src.repositories.protocols.person import PersonListFilters
from src.types import (
    AuditEvent,
    ConnectionType,
    ListedPerson,
    MatchDecision,
    Person,
    PersonConnection,
    PersonEntitySummary,
    PersonGraph,
    PersonIdentifier,
    SourceRecord,
)

from ._utils import record_to_dict, to_total


def _connection_query(connection_type: ConnectionType) -> str:
    if connection_type is ConnectionType.IDENTIFIER:
        return GET_PERSON_CONNECTIONS_IDENTIFIER
    if connection_type is ConnectionType.ADDRESS:
        return GET_PERSON_CONNECTIONS_ADDRESS
    if connection_type is ConnectionType.KNOWS:
        return GET_PERSON_CONNECTIONS_KNOWS
    return GET_PERSON_CONNECTIONS_ALL


def _connection_count_query(connection_type: ConnectionType) -> str:
    if connection_type is ConnectionType.IDENTIFIER:
        return COUNT_PERSON_CONNECTIONS_IDENTIFIER
    if connection_type is ConnectionType.ADDRESS:
        return COUNT_PERSON_CONNECTIONS_ADDRESS
    if connection_type is ConnectionType.KNOWS:
        return COUNT_PERSON_CONNECTIONS_KNOWS
    return COUNT_PERSON_CONNECTIONS_ALL


class Neo4jPersonRepository:
    async def get_page(
        self, filters: PersonListFilters, skip: int, limit: int
    ) -> tuple[list[ListedPerson], int]:
        sort_by = filters.get("sort_by")
        sort_order = filters.get("sort_order")
        has_q = filters.get("q") is not None
        list_query = build_list_persons_query(sort_by, sort_order, has_q=has_q)
        count_query = build_count_persons_query(has_q=has_q)
        # sort_by/sort_order are used to build the query string, not as Cypher params
        cypher_params: dict[str, str | int | bool | None] = {
            k: v  # type: ignore[misc]  # TypedDict values are object; known-safe filter keys
            for k, v in filters.items()
            if k not in ("sort_by", "sort_order")
        }
        list_params = {**cypher_params, "skip": skip, "limit": limit + 1}
        count_params = cypher_params
        async with get_session() as session:
            list_result = await session.run(list_query, list_params)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in list_result]
            count_result = await session.run(count_query, count_params)
            count_record = await count_result.single()
        total = to_total(count_record)
        return [map_listed_person(rec) for rec in records[:limit]], total

    async def search_by_identifier(self, identifier_type: str, value: str) -> list[Person]:
        async with get_session() as session:
            result = await session.run(
                FIND_PERSON_BY_IDENTIFIER, identifier_type=identifier_type, value=value
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_person(rec) for rec in records]

    async def search_by_query(
        self, q: str, status: str | None, skip: int, limit: int
    ) -> tuple[list[Person], bool]:
        async with get_session() as session:
            result = await session.run(
                SEARCH_PERSONS,
                {"query": q, "status": status, "skip": skip, "limit": limit + 1},
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > limit
        return [map_person(rec) for rec in records[:limit]], has_more

    async def get_by_id(self, person_id: str) -> Person | None:
        async with get_session() as session:
            result = await session.run(GET_PERSON_BY_ID, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        return map_person(record_to_dict(record.keys(), list(record.values())))

    async def get_source_records(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[SourceRecord], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_SOURCE_RECORDS, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_SOURCE_RECORDS, person_id=person_id)
            count_record = await count_result.single()
        return [map_source_record(rec) for rec in records[:limit]], to_total(count_record)

    async def get_identifiers(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonIdentifier], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_IDENTIFIERS, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_IDENTIFIERS, person_id=person_id)
            count_record = await count_result.single()
        return [map_person_identifier(rec) for rec in records[:limit]], to_total(count_record)

    async def get_connections(
        self,
        person_id: str,
        connection_type: ConnectionType,
        identifier_type: str | None,
        skip: int,
        limit: int,
    ) -> tuple[list[PersonConnection], int]:
        query = _connection_query(connection_type)
        count_query = _connection_count_query(connection_type)
        async with get_session() as session:
            result = await session.run(
                query,
                person_id=person_id,
                identifier_type=identifier_type,
                skip=skip,
                limit=limit + 1,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(
                count_query, person_id=person_id, identifier_type=identifier_type
            )
            count_record = await count_result.single()
        return [map_connection(rec) for rec in records[:limit]], to_total(count_record)

    async def get_entities(self, person_id: str) -> list[PersonEntitySummary]:
        async with get_session() as session:
            result = await session.run(GET_PERSON_ENTITIES, person_id=person_id)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_person_entity(rec) for rec in records]

    async def get_graph(self, person_id: str, max_hops: int) -> PersonGraph | None:
        query = get_graph_query(max_hops)
        async with get_session() as session:
            result = await session.run(query, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        return map_person_graph(record_to_dict(record.keys(), list(record.values())))

    async def get_node_graph(self, element_id: str, max_hops: int) -> PersonGraph | None:
        query = get_node_graph_query(max_hops)
        async with get_session() as session:
            result = await session.run(query, element_id=element_id)
            record = await result.single()
        if record is None:
            return None
        return map_person_graph(record_to_dict(record.keys(), list(record.values())))

    async def get_audit(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[AuditEvent], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_AUDIT, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_AUDIT, person_id=person_id)
            count_record = await count_result.single()
        return [map_audit_event(rec) for rec in records[:limit]], to_total(count_record)

    async def get_matches(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[MatchDecision], bool]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_MATCHES, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        has_more = len(records) > limit
        return [map_match_decision(rec) for rec in records[:limit]], has_more
