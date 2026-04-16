"""Cypher query constants for the profile-unifier API.

Rules followed:
    - HAS_FACT goes Person -> SourceRecord
    - IDENTIFIED_BY carries source_system_key and source_record_pk on the rel
    - Golden profile fields live directly on the Person node
    - preferred_address_id is resolved to a full Address at read time
    - Reads use parameterised queries; writes belong to route modules and
      MUST use session.execute_write with explicit transactions.

Constants are organized into thematic submodules and re-exported here so
existing call sites (``from src.graph.queries import X``) keep working
unchanged.
"""

from __future__ import annotations

from src.graph.queries.admin import (
    GET_FIELD_TRUST,
    LIST_SOURCE_SYSTEMS,
    UPDATE_FIELD_TRUST,
)
from src.graph.queries.events import LIST_EVENTS
from src.graph.queries.graph import (
    DEFAULT_HOPS,
    MAX_HOPS,
    MIN_HOPS,
    get_graph_query,
    get_node_graph_query,
)
from src.graph.queries.ingestion import (
    CHECK_SOURCE_SYSTEM,
    CREATE_INGEST_RUN,
    CREATE_INGEST_RUN_INLINE,
    CREATE_SOURCE_RECORD,
    GET_INGEST_RUN,
    UPDATE_INGEST_RUN,
    UPDATE_INGEST_RUN_COUNTERS,
)
from src.graph.queries.merge import (
    CHECK_BOTH_PERSONS_ACTIVE,
    CHECK_EXISTING_LOCK,
    CHECK_NO_MATCH_LOCK,
    CREATE_PERSON_PAIR_LOCK,
    CREATE_UNMERGE_AUDIT,
    DELETE_LOCK,
    EXECUTE_MANUAL_MERGE,
    FLAG_AFFECTED_RECORDS_FOR_REVIEW,
    GET_UNMERGE_TARGET,
    REVERT_MERGE,
)
from src.graph.queries.persons import (
    FIND_PERSON_BY_IDENTIFIER,
    GET_PERSON_AUDIT,
    GET_PERSON_BY_ID,
    GET_PERSON_CONNECTIONS_ADDRESS,
    GET_PERSON_CONNECTIONS_ALL,
    GET_PERSON_CONNECTIONS_IDENTIFIER,
    GET_PERSON_CONNECTIONS_KNOWS,
    GET_PERSON_MATCHES,
    GET_PERSON_SOURCE_RECORDS,
    SEARCH_PERSONS,
)
from src.graph.queries.review import (
    ASSIGN_REVIEW_CASE,
    CREATE_NO_MATCH_LOCK_FROM_REVIEW,
    GET_REVIEW_CASE,
    LIST_REVIEW_CASES,
)
from src.graph.queries.sales import GET_PERSON_SALES
from src.graph.queries.survivorship import (
    CHECK_PERSON_ACTIVE,
    CHECK_SOURCE_RECORD_LINKED,
    CREATE_RECOMPUTE_AUDIT,
    GET_BEST_ADDRESS,
    GET_FACT_VALUE,
    GET_PERSON_FACTS,
    GET_PERSON_OVERRIDES,
    GET_PERSON_OVERRIDES_FULL,
    UPDATE_GOLDEN_FIELD,
    UPDATE_GOLDEN_PROFILE,
    UPDATE_OVERRIDES,
)

__all__ = [
    "ASSIGN_REVIEW_CASE",
    "CHECK_BOTH_PERSONS_ACTIVE",
    "CHECK_EXISTING_LOCK",
    "CHECK_NO_MATCH_LOCK",
    "CHECK_PERSON_ACTIVE",
    "CHECK_SOURCE_RECORD_LINKED",
    "CHECK_SOURCE_SYSTEM",
    "CREATE_INGEST_RUN",
    "CREATE_INGEST_RUN_INLINE",
    "CREATE_NO_MATCH_LOCK_FROM_REVIEW",
    "CREATE_PERSON_PAIR_LOCK",
    "CREATE_RECOMPUTE_AUDIT",
    "CREATE_SOURCE_RECORD",
    "CREATE_UNMERGE_AUDIT",
    "DEFAULT_HOPS",
    "DELETE_LOCK",
    "EXECUTE_MANUAL_MERGE",
    "FIND_PERSON_BY_IDENTIFIER",
    "FLAG_AFFECTED_RECORDS_FOR_REVIEW",
    "GET_BEST_ADDRESS",
    "GET_FACT_VALUE",
    "GET_FIELD_TRUST",
    "GET_INGEST_RUN",
    "MAX_HOPS",
    "MIN_HOPS",
    "GET_PERSON_AUDIT",
    "get_graph_query",
    "get_node_graph_query",
    "GET_PERSON_BY_ID",
    "GET_PERSON_CONNECTIONS_ADDRESS",
    "GET_PERSON_CONNECTIONS_ALL",
    "GET_PERSON_CONNECTIONS_IDENTIFIER",
    "GET_PERSON_CONNECTIONS_KNOWS",
    "GET_PERSON_FACTS",
    "GET_PERSON_MATCHES",
    "GET_PERSON_OVERRIDES",
    "GET_PERSON_OVERRIDES_FULL",
    "GET_PERSON_SALES",
    "GET_PERSON_SOURCE_RECORDS",
    "GET_REVIEW_CASE",
    "GET_UNMERGE_TARGET",
    "LIST_EVENTS",
    "LIST_REVIEW_CASES",
    "LIST_SOURCE_SYSTEMS",
    "REVERT_MERGE",
    "SEARCH_PERSONS",
    "UPDATE_FIELD_TRUST",
    "UPDATE_GOLDEN_FIELD",
    "UPDATE_GOLDEN_PROFILE",
    "UPDATE_INGEST_RUN",
    "UPDATE_INGEST_RUN_COUNTERS",
    "UPDATE_OVERRIDES",
]
