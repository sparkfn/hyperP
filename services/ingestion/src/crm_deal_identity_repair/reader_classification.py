"""Explicit #310 reader classification for repair-retired relationship evidence.

Current-authority readers must ignore explicitly inactive edges while accepting
legacy edges without an ``is_active`` property.  Audit/history/lineage readers
intentionally retain both sides of the lifecycle so repair evidence remains
explainable.  Mutation orchestration is recorded separately to keep the
completeness check from mistaking writes for authority reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ReaderClass = Literal["current_authority", "audit_history_lineage"]


@dataclass(frozen=True)
class ReaderClassification:
    """One reviewed relationship reader with its intentional semantic class."""

    module_path: str
    query_name: str
    reader_class: ReaderClass
    relationship_types: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class NonReaderRelationshipQuery:
    """A relationship query deliberately excluded because it mutates or wires evidence."""

    module_path: str
    query_name: str
    rationale: str


def _current(
    module_path: str,
    query_names: tuple[str, ...],
    relationship_types: tuple[str, ...],
    rationale: str,
) -> tuple[ReaderClassification, ...]:
    return tuple(
        ReaderClassification(module_path, name, "current_authority", relationship_types, rationale)
        for name in query_names
    )


def _audit(
    module_path: str,
    query_names: tuple[str, ...],
    relationship_types: tuple[str, ...],
    rationale: str,
) -> tuple[ReaderClassification, ...]:
    return tuple(
        ReaderClassification(
            module_path,
            name,
            "audit_history_lineage",
            relationship_types,
            rationale,
        )
        for name in query_names
    )


def _non_reader(
    module_path: str,
    query_names: tuple[str, ...],
    rationale: str,
) -> tuple[NonReaderRelationshipQuery, ...]:
    return tuple(
        NonReaderRelationshipQuery(module_path, name, rationale) for name in query_names
    )


CURRENT_AUTHORITY_READERS: tuple[ReaderClassification, ...] = (
    *_current(
        "services/api/src/graph/queries/persons.py",
        (
            "FIND_PERSON_BY_IDENTIFIER", "GET_PERSON_BY_ID", "GET_PERSON_LOYALTY",
            "GET_PERSON_VEHICLES", "GET_PERSON_SOURCE_RECORD_ENTITY_FACETS",
            "GET_PERSON_CONNECTIONS_IDENTIFIER", "GET_PERSON_CONNECTIONS_ADDRESS",
            "GET_PERSON_CONNECTIONS_KNOWS", "GET_PERSON_CONNECTIONS_ALL", "SEARCH_PERSONS",
            "GET_PERSON_ENTITIES", "GET_PERSON_SHARED_IDENTIFIERS",
            "COUNT_PERSON_SOURCE_RECORDS", "COUNT_PERSON_SHARED_IDENTIFIERS",
            "COUNT_PERSON_CONNECTIONS_IDENTIFIER",
            "COUNT_PERSON_CONNECTIONS_ADDRESS", "COUNT_PERSON_CONNECTIONS_KNOWS",
            "COUNT_PERSON_CONNECTIONS_ALL", "GET_PERSON_POSSIBLE_MATCH_DETAIL",
        ),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "KNOWS", "PURCHASED"),
        "Person detail, list-derived metrics, and connection authority are current-state reads.",
    ),
    *_current(
        "services/api/src/graph/queries/persons_list.py",
        ("GET_PERSON_LIST_SUMMARY", "_SOURCE_RECORD_COUNT", "_ENTITY_ENRICHMENT", "_ENTITY_COUNT",
         "_CONNECTION_COUNT", "_PHONE_CONFIDENCE", "_IDENTIFIER_COUNT",
         "_POSSIBLE_MATCH_COUNT", "_ORDER_COUNT", "build_list_persons_query",
         "build_count_persons_query"),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "KNOWS", "PURCHASED"),
        "Person list, counts, filters, and metrics select current operational evidence.",
    ),
    *_current(
        "services/api/src/graph/queries/entities.py",
        ("LIST_ENTITIES", "LIST_FILTER_SOURCE_SYSTEMS", "_ENTITY_PERSONS_HEAD",
         "_SOURCE_RECORD_COUNT", "_CONNECTION_COUNT", "_PHONE_CONFIDENCE",
         "get_entity_persons_query"),
        ("HAS_FACT", "IDENTIFIED_BY", "LIVES_AT", "KNOWS"),
        "Entity reporting and entity-person membership are current operational projections.",
    ),
    *_current(
        "services/api/src/graph/queries/survivorship.py",
        (
            "GET_PERSON_FACTS",
            "GET_BEST_ADDRESS",
            "GET_BEST_IDENTIFIER",
            "CHECK_SOURCE_RECORD_LINKED",
            "GET_FACT_VALUE",
            "GET_IDENTIFIER_VALUE_FOR_SR",
            "GET_ADDRESS_FOR_SR",
            "GET_FIELD_OPTIONS",
        ),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT"),
        "Survivorship and owner inheritance must only use presently authoritative evidence.",
    ),
    *_current(
        "services/api/src/graph/queries/sales.py",
        ("GET_PERSON_SALES", "COUNT_PERSON_SALES"),
        ("PURCHASED",),
        "Sales detail and count endpoints are current customer views.",
    ),
    *_current(
        "services/api/src/graph/queries/reports.py",
        (
            "SEED_REPORTS.entity_person_summary",
            "SEED_REPORTS.shared_phone_numbers",
            "SEED_REPORTS.top_buyers",
        ),
        ("HAS_FACT", "IDENTIFIED_BY", "LIVES_AT", "PURCHASED"),
        "Seeded operational reports must not count repair-retired links.",
    ),
    *_current(
        "services/api/src/graph/queries/sales_prediction_discovery.py",
        ("DISCOVERY_DEAL_RECORDS",),
        ("LINKED_TO",),
        "Discovery retains record-version history but linked-person authority is current only.",
    ),
    *_current(
        "services/api/src/graph/queries/sales_prediction_gate.py",
        ("GATE_DEAL_VERSIONS_FOR_PARENTS",),
        ("LINKED_TO",),
        "Gate inputs retain deal versions but exclude retired person associations from metrics.",
    ),
    *_current(
        "services/api/src/graph/queries/profile_analysis.py",
        ("GET_PERSON_PROFILE_ANALYSES", "CREATE_PROFILE_ANALYSIS_REQUEST",
         "CREATE_FAILED_PROFILE_ANALYSIS_RETRY"),
        ("CURRENT_PROFILE_ANALYSIS",),
        "Current profile-analysis pointers are operational state, not history.",
    ),
    *_current(
        "services/api/src/profile_analysis_runtime_queries.py",
        ("FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS", "FETCH_PROFILE_ANALYSIS_SENSITIVE_VALUES"),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "PURCHASED", "KNOWS",
         "OWNS_VEHICLE", "BOUGHT_VEHICLE", "MENTIONS_VEHICLE"),
        "Profile-analysis runtime snapshots and dirty inputs must exclude retired evidence.",
    ),
    *_current(
        "services/api/src/graph/queries/users.py",
        ("GET_ENTITIES_FOR_REVIEW_CASE",),
        ("LINKED_TO",),
        (
            "Review-case entity authorization is a security decision and cannot traverse "
            "retired links."
        ),
    ),
    *_current(
        "services/ingestion/src/graph/queries/crm_deal_count.py",
        ("_AUTHORITY_MATCH", "RECOMPUTE_PERSON_CRM_DEAL_COUNTS",
         "RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS", "BACKFILL_CRM_DEAL_COUNTS_BATCH",
         "CRM_DEAL_COUNT_INVARIANT_COUNTS"),
        ("LINKED_TO",),
        "CRM deal count projection and its readiness invariant use current ownership only.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/knows.py",
        ("RESOLVE_KNOWS_ENDPOINTS", "RESOLVE_PERSON_FROM_SOURCE_RECORD_ID",
         "RESOLVE_PERSON_FROM_SOURCE_RECORD_PK", "SCAN_CONTACT_SOURCE_RECORDS",
         "SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS"),
        ("LINKED_TO", "KNOWS"),
        "KNOWS materialization resolves only currently linked endpoints and active projections.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/profile_analysis_dirty.py",
        ("MARK_PROFILE_ANALYSIS_DIRTY", "FIND_PROFILE_ANALYSIS_MERGE_AFFECTED_PERSON_IDS"),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "PURCHASED", "KNOWS",
         "OWNS_VEHICLE", "BOUGHT_VEHICLE"),
        "Dirty/runtime recomputation is operational and must react only to current evidence.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/source_records.py",
        ("LOCK_AND_GET_SOURCE_STATE",),
        ("LINKED_TO",),
        "Lifecycle continuity uses the active record-to-person authority link.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/matching.py",
        ("FIND_CANDIDATES_BY_IDENTIFIER", "FIND_CANDIDATES_BY_IDENTIFIERS_BATCH",
         "FIND_CANDIDATES_BY_ADDRESS", "FIND_CANDIDATES_BY_ADDRESSES_BATCH",
         "CHECK_IDENTIFIER_FANOUT"),
        ("IDENTIFIED_BY", "LIVES_AT"),
        "Matching candidate selection and fanout limits are current operational authority.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/person_pairs.py",
        ("FIND_PERSONS_SHARING_IDENTIFIER", "FIND_PERSONS_SHARING_IDENTIFIERS_BATCH"),
        ("IDENTIFIED_BY",),
        "Pair-audit discovery is operational candidate generation, not historical review output.",
    ),
    *_current(
        "services/ingestion/src/matching/deterministic.py",
        ("_FIND_ACTIVE_NO_MATCH_LOCKS", "_PERSON_HAS_IDENTIFIER", "_PERSON_HAS_VALID_GOVT_ID",
         "_PERSON_HAS_CONFLICTING_GOVT_ID"),
        ("IDENTIFIED_BY",),
        "Deterministic matching and safety locks require current identifier authority.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/persons.py",
        ("FETCH_PERSON_FACTS", "FETCH_PERSON_IDENTIFIERS", "FETCH_PERSON_ADDRESSES",
         "FETCH_PERSON_MATCH_IDENTIFIERS", "FETCH_PERSON_MATCH_FACTS",
         "FETCH_PERSON_MATCH_ADDRESSES"),
        ("IDENTIFIED_BY", "LIVES_AT", "HAS_FACT"),
        "Ingestion golden-profile and matching inputs are current evidence readers.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/sales.py",
        ("RESOLVE_SALES_CUSTOMER", "FIND_VEHICLE_CANDIDATES_FOR_SALES"),
        ("LINKED_TO", "OWNS_VEHICLE", "BOUGHT_VEHICLE"),
        "Sales identity resolution and vehicle discovery use active customer authority.",
    ),
    *_current(
        "services/ingestion/src/graph/queries/crm_history.py",
        ("CREATE_CALL_FROM_HISTORY", "ACTIVATE_PENDING_CALLS_FOR_DEAL"),
        ("LINKED_TO",),
        "CRM activity owner inheritance is a current operational projection.",
    ),
)

AUDIT_HISTORY_LINEAGE_READERS: tuple[ReaderClassification, ...] = (
    *_audit(
        "services/api/src/graph/queries/persons.py",
        (
            "GET_PERSON_SOURCE_RECORDS",
            "GET_PERSON_TIMELINE",
            "COUNT_PERSON_TIMELINE",
            "GET_PERSON_TIMELINE_TARGET",
            "GET_PERSON_AUDIT",
            "COUNT_PERSON_AUDIT",
            "GET_PERSON_IDENTIFIERS",
            "COUNT_PERSON_IDENTIFIERS",
        ),
        ("LINKED_TO", "HAS_FACT", "PURCHASED", "IDENTIFIED_BY"),
        (
            "Timeline, audit, record-history, and identifier-history endpoints intentionally "
            "preserve lifecycle evidence."
        ),
    ),
    *_audit(
        "services/api/src/graph/queries/profile_analysis.py",
        ("GET_PERSON_PROFILE_ANALYSIS_HISTORY",),
        ("HAS_PROFILE_ANALYSIS",),
        "Profile-analysis history is immutable audit evidence and retains superseded attempts.",
    ),
    *_audit(
        "services/api/src/graph/queries/merge.py",
        ("EXECUTE_MANUAL_MERGE", "REVERT_MERGE"),
        ("KNOWS", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT"),
        "Merge/unmerge reads and rewires must retain retired provenance for reversal and lineage.",
    ),
    *_audit(
        "services/api/src/graph/queries/review.py",
        ("GET_PENDING_REVIEW_RECORD", "ACTIVATE_PENDING_REVIEW_RECORD",
         "RESOLVE_PENDING_REVIEW_RECORD_NO_MATCH", "REJECT_PENDING_REVIEW_RECORD"),
        ("LINKED_TO", "PURCHASED", "KNOWS"),
        (
            "Review decisions preserve pending and historic evidence; authorization is "
            "classified separately."
        ),
    ),
    *_audit(
        "services/ingestion/src/graph/queries/crm_deal_identity_repair.py",
        ("INVENTORY_ACTIVE_CRM_DEALS", "INVENTORY_CRM_DEAL_PROJECTIONS",
         "INVENTORY_STALE_RUN_CONTROL_PLANE"),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "PURCHASED", "KNOWS"),
        (
            "Repair inventory is deliberately evidence-complete, including inactive metadata "
            "and retired rows."
        ),
    ),
    *_audit(
        "services/ingestion/src/graph/queries/crm_history.py",
        ("FIND_ANY_SOURCE_RECORD", "REMATERIALIZE_CRM_HISTORY_PROJECTION",
         "LINK_CONVERSATION_TO_CRM_HISTORY", "LINK_CRM_HISTORY_TO_EXISTING_CONVERSATIONS"),
        ("LINKED_TO",),
        (
            "History reconstruction and relationship lineage retain prior activity evidence "
            "intentionally."
        ),
    ),
    *_audit(
        "services/ingestion/src/graph/queries/merge.py",
        ("GET_AFFECTED_SOURCE_RECORDS",),
        ("LINKED_TO", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT"),
        "Merge-event affected-record audit retains complete provenance for later review.",
    ),
)

NON_READER_RELATIONSHIP_QUERIES: tuple[NonReaderRelationshipQuery, ...] = (
    *_non_reader(
        "services/api/src/graph/queries/review.py",
        (
            "LINK_REVIEW_SALES_PURCHASED_ORDER",
            "LINK_REVIEW_SALES_BOUGHT_VEHICLE",
            "PROMOTE_STAGED_REVIEW_SALE",
        ),
        "review mutation",
    ),
    *_non_reader(
        "services/api/src/profile_analysis_runtime_queries.py",
        ("PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS",),
        "profile-analysis publication mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/knows.py",
        ("LINK_PERSON_KNOWS", "REWIRE_KNOWS_OUT", "REWIRE_KNOWS_IN"),
        "projection or merge mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/knows.py",
        ("RETIRE_KNOWS_PROJECTION",),
        "retirement mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/profile_analysis_dirty.py",
        ("RETIRE_SOURCE_EVIDENCE",),
        "retirement mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/source_records.py",
        (
            "SUPERSEDE_SOURCE_RECORD",
            "RETIRE_IDENTITY_PROJECTIONS",
            "RETIRE_IDENTITY_PROJECTIONS_FOR_PERSONS",
        ),
        "lifecycle retirement mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/source_records.py",
        ("LINK_SOURCE_RECORD_TO_PERSON",),
        "identity-link mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/persons.py",
        ("LINK_PERSON_TO_IDENTIFIER", "LINK_PERSON_TO_ADDRESS", "CREATE_ATTRIBUTE_FACT"),
        "projection mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/merge.py",
        (
            "REWIRE_LINKED_TO",
            "REWIRE_IDENTIFIED_BY",
            "REWIRE_LIVES_AT",
            "REWIRE_HAS_FACT",
        ),
        "merge mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/sales.py",
        ("LINK_PERSON_PURCHASED_ORDER",),
        "projection mutation",
    ),
    *_non_reader(
        "services/ingestion/src/graph/queries/sales.py",
        (
            "CLEAR_SUPERSEDED_SALES_LINKS",
            "REWIRE_PURCHASED",
            "REWIRE_BOUGHT_VEHICLE",
            "REWIRE_OWNS_VEHICLE",
        ),
        "retirement or merge mutation",
    ),
)

READER_CLASSIFICATIONS: tuple[ReaderClassification, ...] = (
    *CURRENT_AUTHORITY_READERS,
    *AUDIT_HISTORY_LINEAGE_READERS,
)
REPAIR_RETIRABLE_RELATIONSHIP_TYPES: frozenset[str] = frozenset(
    {
        relationship_type
        for item in READER_CLASSIFICATIONS
        for relationship_type in item.relationship_types
    }
)
RELEVANT_READER_MODULES: frozenset[str] = frozenset(
    item.module_path for item in (*READER_CLASSIFICATIONS, *NON_READER_RELATIONSHIP_QUERIES)
)
