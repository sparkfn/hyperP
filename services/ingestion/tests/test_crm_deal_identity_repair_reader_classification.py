"""Static completeness and parity coverage for #310 reader safety."""

from __future__ import annotations

import ast
from pathlib import Path

from src.crm_deal_identity_repair.reader_classification import (
    AUDIT_HISTORY_LINEAGE_READERS,
    CURRENT_AUTHORITY_READERS,
    NON_READER_RELATIONSHIP_QUERIES,
    READER_CLASSIFICATIONS,
    RELEVANT_READER_MODULES,
)

_REPAIR_RELATIONSHIPS = (
    "LINKED_TO", "PURCHASED", "IDENTIFIED_BY", "LIVES_AT", "HAS_FACT", "KNOWS",
    "OWNS_VEHICLE", "BOUGHT_VEHICLE", "MENTIONS_VEHICLE", "CURRENT_PROFILE_ANALYSIS",
)
_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_MANDATORY_READER_KEYS = frozenset(
    {
        ("services/api/src/graph/queries/persons_list.py", "build_list_persons_query"),
        ("services/api/src/graph/queries/persons_list.py", "build_count_persons_query"),
        ("services/api/src/graph/queries/entities.py", "get_entity_persons_query"),
        ("services/api/src/graph/queries/survivorship.py", "GET_BEST_IDENTIFIER"),
        ("services/api/src/graph/queries/sales.py", "GET_PERSON_SALES"),
        ("services/api/src/graph/queries/reports.py", "SEED_REPORTS.top_buyers"),
        ("services/api/src/graph/queries/sales_prediction_discovery.py", "DISCOVERY_DEAL_RECORDS"),
        (
            "services/api/src/graph/queries/sales_prediction_gate.py",
            "GATE_DEAL_VERSIONS_FOR_PARENTS",
        ),
        ("services/api/src/graph/queries/profile_analysis.py", "GET_PERSON_PROFILE_ANALYSES"),
        (
            "services/api/src/profile_analysis_runtime_queries.py",
            "FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS",
        ),
        ("services/api/src/graph/queries/users.py", "GET_ENTITIES_FOR_REVIEW_CASE"),
        ("services/ingestion/src/graph/queries/crm_deal_count.py", "_AUTHORITY_MATCH"),
        ("services/ingestion/src/graph/queries/knows.py", "RESOLVE_KNOWS_ENDPOINTS"),
        (
            "services/ingestion/src/graph/queries/profile_analysis_dirty.py",
            "MARK_PROFILE_ANALYSIS_DIRTY",
        ),
        ("services/ingestion/src/graph/queries/source_records.py", "LOCK_AND_GET_SOURCE_STATE"),
        ("services/ingestion/src/graph/queries/matching.py", "FIND_CANDIDATES_BY_IDENTIFIER"),
        ("services/ingestion/src/graph/queries/person_pairs.py", "FIND_PERSONS_SHARING_IDENTIFIER"),
        ("services/ingestion/src/matching/deterministic.py", "_PERSON_HAS_VALID_GOVT_ID"),
        ("services/ingestion/src/graph/queries/persons.py", "FETCH_PERSON_MATCH_IDENTIFIERS"),
        ("services/ingestion/src/graph/queries/sales.py", "RESOLVE_SALES_CUSTOMER"),
        ("services/ingestion/src/graph/queries/crm_history.py", "CREATE_CALL_FROM_HISTORY"),
        ("services/api/src/graph/queries/persons.py", "GET_PERSON_IDENTIFIERS"),
        (
            "services/api/src/graph/queries/profile_analysis.py",
            "GET_PERSON_PROFILE_ANALYSIS_HISTORY",
        ),
        ("services/api/src/graph/queries/review.py", "GET_PENDING_REVIEW_RECORD"),
        (
            "services/ingestion/src/graph/queries/crm_deal_identity_repair.py",
            "INVENTORY_ACTIVE_CRM_DEALS",
        ),
    }
)


def _direct_query_constants(module_path: str) -> dict[str, str]:
    module = ast.parse((_REPOSITORY_ROOT / module_path).read_text(encoding="utf-8-sig"))
    values: dict[str, str] = {}
    for statement in module.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not names or not names[0].isupper() or not isinstance(statement.value, ast.Constant):
            continue
        if isinstance(statement.value.value, str):
            values[names[0]] = statement.value.value
    return values


def _classified_keys() -> set[tuple[str, str]]:
    return {(item.module_path, item.query_name) for item in READER_CLASSIFICATIONS}


def test_every_reviewed_relationship_query_has_one_explicit_disposition() -> None:
    classified = _classified_keys()
    excluded = {(item.module_path, item.query_name) for item in NON_READER_RELATIONSHIP_QUERIES}
    assert classified.isdisjoint(excluded)
    assert len(classified) == len(READER_CLASSIFICATIONS)
    assert len(excluded) == len(NON_READER_RELATIONSHIP_QUERIES)
    assert _MANDATORY_READER_KEYS <= classified

    discovered: set[tuple[str, str]] = set()
    for module_path in RELEVANT_READER_MODULES:
        for query_name, query in _direct_query_constants(module_path).items():
            if "MATCH" in query and any(rel_type in query for rel_type in _REPAIR_RELATIONSHIPS):
                discovered.add((module_path, query_name))
    assert discovered <= classified | excluded


def test_current_authority_inventory_requires_rollout_compatible_filters() -> None:
    for item in CURRENT_AUTHORITY_READERS:
        assert item.rationale
        assert item.relationship_types
        constants = _direct_query_constants(item.module_path)
        query = constants.get(item.query_name)
        if query is None:
            continue
        for relationship_type in item.relationship_types:
            if relationship_type in query:
                assert "coalesce(" in query
                assert ".is_active, true) = true" in query


def test_audit_history_allowlist_is_explicit_and_preserves_repair_evidence() -> None:
    assert all(item.rationale for item in AUDIT_HISTORY_LINEAGE_READERS)
    inventory = _direct_query_constants(
        "services/ingestion/src/graph/queries/crm_deal_identity_repair.py"
    )["INVENTORY_ACTIVE_CRM_DEALS"]
    assert "relationship_properties: properties(link)" in inventory
    assert "relationship_is_active: coalesce(descendant_link.is_active, true)" in inventory
    assert "WHERE coalesce(link.is_active, true) = true" not in inventory


def test_api_and_ingestion_current_reader_parity_covers_repair_relationships() -> None:
    api_runtime_path = _REPOSITORY_ROOT / "services/api/src/profile_analysis_runtime_queries.py"
    api_runtime = api_runtime_path.read_text(
        encoding="utf-8-sig"
    )
    ingestion_dirty = (
        _REPOSITORY_ROOT / "services/ingestion/src/graph/queries/profile_analysis_dirty.py"
    ).read_text(encoding="utf-8-sig")
    api_persons = (_REPOSITORY_ROOT / "services/api/src/graph/queries/persons.py").read_text(
        encoding="utf-8-sig"
    )
    ingestion_persons = (
        _REPOSITORY_ROOT / "services/ingestion/src/graph/queries/persons.py"
    ).read_text(encoding="utf-8-sig")
    relationship_types = (
        "LINKED_TO",
        "IDENTIFIED_BY",
        "LIVES_AT",
        "HAS_FACT",
        "PURCHASED",
        "KNOWS",
    )
    for relationship_type in relationship_types:
        assert relationship_type in api_runtime
        assert relationship_type in ingestion_dirty
    for relationship_type in ("IDENTIFIED_BY", "LIVES_AT", "HAS_FACT"):
        assert relationship_type in api_persons
        assert relationship_type in ingestion_persons
    assert api_runtime.count(".is_active, true) = true") >= 12
    assert ingestion_dirty.count(".is_active, true) = true") >= 12


def test_security_and_current_pointer_readers_name_and_filter_retirable_edges() -> None:
    users = _direct_query_constants("services/api/src/graph/queries/users.py")[
        "GET_ENTITIES_FOR_REVIEW_CASE"
    ]
    profile_analysis = (
        _REPOSITORY_ROOT / "services/api/src/graph/queries/profile_analysis.py"
    ).read_text(encoding="utf-8-sig")
    discovery = _direct_query_constants(
        "services/api/src/graph/queries/sales_prediction_discovery.py"
    )["DISCOVERY_DEAL_RECORDS"]
    gate = _direct_query_constants("services/api/src/graph/queries/sales_prediction_gate.py")[
        "GATE_DEAL_VERSIONS_FOR_PARENTS"
    ]
    assert "[source_link:LINKED_TO]" in users
    assert "[person_link:LINKED_TO]" in users
    assert "[person_source_link:LINKED_TO]" in users
    assert users.count(".is_active, true) = true") == 3
    assert profile_analysis.count("CURRENT_PROFILE_ANALYSIS") >= 4
    assert profile_analysis.count(".is_active, true) = true") >= 4
    assert "[link:LINKED_TO]" in discovery
    assert "coalesce(link.is_active, true) = true" in discovery
    assert "[link:LINKED_TO]" in gate
    assert "coalesce(link.is_active, true) = true" in gate


def test_seed_reports_and_knows_materialization_ignore_retired_evidence() -> None:
    reports = (_REPOSITORY_ROOT / "services/api/src/graph/queries/reports.py").read_text(
        encoding="utf-8-sig"
    )
    knows = _direct_query_constants("services/ingestion/src/graph/queries/knows.py")[
        "RESOLVE_KNOWS_ENDPOINTS"
    ]
    assert "[purchase:PURCHASED]" in reports
    assert "coalesce(purchase.is_active, true) = true" in reports
    assert "[declarer_link:LINKED_TO]" in knows
    assert "[contact_link:LINKED_TO]" in knows
    assert knows.count(".is_active, true) = true") == 2
