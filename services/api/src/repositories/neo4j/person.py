"""Neo4j implementation of PersonRepository."""

from __future__ import annotations

import asyncio

from src.graph.client import get_session
from src.graph.converters import GraphRecord
from src.graph.mappers import (
    map_audit_event,
    map_bankruptcy_case,
    map_connection,
    map_match_decision,
    map_person,
    map_person_graph,
    map_person_identifier,
    map_possible_match_detail,
    map_shared_identifier_candidate,
    map_source_record,
    map_source_record_entity_facet,
    map_timeline_group,
)
from src.graph.mappers_entities import map_listed_person, map_person_entity
from src.graph.mappers_profile_analysis import (
    map_person_profile_analyses,
    map_profile_analysis_history_item,
)
from src.graph.queries import (
    COUNT_PERSON_AUDIT,
    COUNT_PERSON_BANKRUPTCY_CASES,
    COUNT_PERSON_CONNECTIONS_ADDRESS,
    COUNT_PERSON_CONNECTIONS_ALL,
    COUNT_PERSON_CONNECTIONS_IDENTIFIER,
    COUNT_PERSON_CONNECTIONS_KNOWS,
    COUNT_PERSON_IDENTIFIERS,
    COUNT_PERSON_MATCHES,
    COUNT_PERSON_SHARED_IDENTIFIERS,
    COUNT_PERSON_SOURCE_RECORDS,
    COUNT_PERSON_TIMELINE,
    FIND_PERSON_BY_IDENTIFIER,
    GET_PERSON_AUDIT,
    GET_PERSON_BANKRUPTCY_CASES,
    GET_PERSON_BY_ID,
    GET_PERSON_CONNECTIONS_ADDRESS,
    GET_PERSON_CONNECTIONS_ALL,
    GET_PERSON_CONNECTIONS_IDENTIFIER,
    GET_PERSON_CONNECTIONS_KNOWS,
    GET_PERSON_ENTITIES,
    GET_PERSON_IDENTIFIERS,
    GET_PERSON_MATCHES,
    GET_PERSON_POSSIBLE_MATCH_DETAIL,
    GET_PERSON_PROFILE_ANALYSES,
    GET_PERSON_PROFILE_ANALYSIS_HISTORY,
    GET_PERSON_SHARED_IDENTIFIERS,
    GET_PERSON_SOURCE_RECORD_ENTITY_FACETS,
    GET_PERSON_SOURCE_RECORDS,
    GET_PERSON_TIMELINE,
    GET_PERSON_TIMELINE_TARGET,
    SEARCH_PERSONS,
    build_count_persons_query,
    build_list_persons_query,
    get_graph_query,
    get_node_graph_query,
)
from src.repositories.protocols.person import PersonListFilters
from src.types import (
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
    PossibleMatchDetail,
    SourceRecord,
    SourceRecordEntityFacet,
)
from src.types_profile_analysis import (
    PersonProfileAnalyses,
    ProfileAnalysisHistoryItem,
    ProfileAnalysisType,
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
        has_addr_filter = any(
            filters.get(k) is not None
            for k in ("addr_street", "addr_unit", "addr_city", "addr_postal", "addr_country")
        )
        entity_mode = filters.get("entity_key_mode") or "or"
        source_mode = filters.get("source_key_mode") or "or"
        list_query = build_list_persons_query(
            sort_by, sort_order, has_q=has_q, entity_mode=entity_mode, source_mode=source_mode
        )
        count_query = build_count_persons_query(
            has_q=has_q,
            has_addr_filter=has_addr_filter,
            entity_mode=entity_mode,
            source_mode=source_mode,
        )
        # sort_by/sort_order/entity_key_mode/source_key_mode are used to build
        # the query string, not as Cypher params
        cypher_params: dict[str, str | int | bool | list[str] | None] = {
            k: v  # type: ignore[misc]  # TypedDict values are object; known-safe filter keys
            for k, v in filters.items()
            if k not in ("sort_by", "sort_order", "entity_key_mode", "source_key_mode")
        }
        list_params = {**cypher_params, "skip": skip, "limit": limit + 1}
        count_params = cypher_params

        async def _run_list() -> list[GraphRecord]:
            async with get_session() as session:
                result = await session.run(list_query, list_params)
                return [record_to_dict(r.keys(), list(r.values())) async for r in result]

        async def _run_count() -> int:
            async with get_session() as session:
                result = await session.run(count_query, count_params)
                record = await result.single()
                return to_total(record)

        records, total = await asyncio.gather(_run_list(), _run_count())
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

    async def get_profile_analyses(self, person_id: str) -> PersonProfileAnalyses | None:
        async with get_session() as session:
            result = await session.run(GET_PERSON_PROFILE_ANALYSES, person_id=person_id)
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        return map_person_profile_analyses(mapped)

    async def get_profile_analysis_history(
        self,
        person_id: str,
        analysis_type: ProfileAnalysisType | None,
        skip: int,
        limit: int,
    ) -> tuple[list[ProfileAnalysisHistoryItem], int] | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_PROFILE_ANALYSIS_HISTORY,
                person_id=person_id,
                analysis_type=analysis_type,
                skip=skip,
                limit=limit,
            )
            record = await result.single()
        if record is None:
            return None
        mapped = record_to_dict(record.keys(), list(record.values()))
        raw_analyses = mapped.get("analyses")
        if not isinstance(raw_analyses, list):
            raise TypeError("profile analysis history query returned invalid analyses")
        items: list[ProfileAnalysisHistoryItem] = []
        for raw_analysis in raw_analyses:
            if not isinstance(raw_analysis, dict):
                raise TypeError("profile analysis history query returned invalid analysis")
            items.append(map_profile_analysis_history_item({"analysis": raw_analysis}))
        return items, to_total(record)

    async def get_source_records(
        self,
        person_id: str,
        skip: int,
        limit: int,
        entity_key: str | None = None,
        record_type: str | None = None,
    ) -> tuple[list[SourceRecord], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_SOURCE_RECORDS,
                person_id=person_id,
                skip=skip,
                limit=limit + 1,
                entity_key=entity_key,
                record_type=record_type,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(
                COUNT_PERSON_SOURCE_RECORDS,
                person_id=person_id,
                entity_key=entity_key,
                record_type=record_type,
            )
            count_record = await count_result.single()
        return [map_source_record(rec) for rec in records[:limit]], to_total(count_record)

    async def get_source_record_entity_facets(
        self, person_id: str
    ) -> list[SourceRecordEntityFacet]:
        async with get_session() as session:
            result = await session.run(GET_PERSON_SOURCE_RECORD_ENTITY_FACETS, person_id=person_id)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_source_record_entity_facet(rec) for rec in records]

    async def get_bankruptcy_cases(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[BankruptcyCase], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_BANKRUPTCY_CASES, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_BANKRUPTCY_CASES, person_id=person_id)
            count_record = await count_result.single()
        return [map_bankruptcy_case(rec) for rec in records[:limit]], to_total(count_record)

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

    async def get_shared_identifier_candidates(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonSharedIdentifierCandidate], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_SHARED_IDENTIFIERS,
                person_id=person_id,
                skip=skip,
                limit=limit + 1,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_SHARED_IDENTIFIERS, person_id=person_id)
            count_record = await count_result.single()
        candidates = [map_shared_identifier_candidate(rec) for rec in records[:limit]]
        return candidates, to_total(count_record)

    async def get_possible_match_detail(
        self, person_id: str, candidate_person_id: str
    ) -> PossibleMatchDetail | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_POSSIBLE_MATCH_DETAIL,
                person_id=person_id,
                candidate_person_id=candidate_person_id,
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        if not records:
            return None
        return map_possible_match_detail(records)

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
    ) -> tuple[list[MatchDecision], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_MATCHES, person_id=person_id, skip=skip, limit=limit
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_MATCHES, person_id=person_id)
            count_record = await count_result.single()
        return [map_match_decision(rec) for rec in records], to_total(count_record)

    async def get_timeline(
        self, person_id: str, skip: int, limit: int
    ) -> tuple[list[PersonTimelineGroup], int]:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_TIMELINE, person_id=person_id, skip=skip, limit=limit + 1
            )
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
            count_result = await session.run(COUNT_PERSON_TIMELINE, person_id=person_id)
            count_record = await count_result.single()
        return [map_timeline_group(rec) for rec in records[:limit]], to_total(count_record)

    async def get_timeline_target(
        self, person_id: str, source_record_pk: str
    ) -> PersonTimelineGroup | None:
        async with get_session() as session:
            result = await session.run(
                GET_PERSON_TIMELINE_TARGET,
                person_id=person_id,
                source_record_pk=source_record_pk,
            )
            record = await result.single()
        if record is None:
            return None
        return map_timeline_group(record_to_dict(record.keys(), list(record.values())))
