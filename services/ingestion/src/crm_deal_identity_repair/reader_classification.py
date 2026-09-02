"""Exhaustive source-level classification of executable relationship readers.

The #310 repair preserves relationship history by retiring projections instead of
removing them.  Current-state readers must therefore explicitly filter inactive
relationships.  This module makes that policy fail closed: every discovered
read query touching a repairable relationship type must be named in one of the
explicit classification sets below.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

ReaderClass = Literal["authoritative", "audit"]

_RELATIONSHIP_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:LINKED_TO|IDENTIFIED_BY|LIVES_AT|HAS_FACT|KNOWS|PURCHASED|"
    r"BOUGHT_VEHICLE|OWNS_VEHICLE|MENTIONS_VEHICLE)\b"
)
_READ_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:OPTIONAL\s+)?MATCH\b")
_WRITE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\b(?:CREATE|MERGE|SET|DELETE|REMOVE)\b")
_ACTIVE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"coalesce\s*\([^)]*\.is_active\s*,\s*true\s*\)\s*=\s*true"
)

# These sets are intentionally complete, not heuristic.  Adding a query binding
# that reads a repairable relationship must update this contract and its tests.
_AUDIT_READERS: Final[frozenset[str]] = frozenset(
    {
        "api/graph/queries/review.py:GET_PENDING_REVIEW_RECORD",
        "ingestion/graph/queries/crm_deal_identity_repair.py:INVENTORY_ACTIVE_CRM_DEALS",
        "ingestion/graph/queries/crm_deal_identity_repair.py:INVENTORY_CRM_DEAL_PROJECTIONS",
        "ingestion/graph/queries/crm_deal_identity_repair_mutation.py:READ_REPAIRED_OWNER_IDS",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_AFFECTED_PERSON_IDS",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_APPLIED_REPLACEMENT_OWNER",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_EXPECTED_AFFECTED_CRM_DEAL_COUNTS",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_NEGATIVE_CONTROL_FULL_STATE",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_NEGATIVE_CONTROL_SNAPSHOT",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_PAIR_BRIDGE",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_PRIMARY_POSTCONDITIONS",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_RETIRED_RELATIONSHIP_SNAPSHOTS",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_RUN_GRAPH_TOTALS",
        "ingestion/graph/queries/crm_deal_identity_repair_verification.py:READ_SECONDARY_CONTEXT",
        "ingestion/graph/queries/merge.py:GET_AFFECTED_SOURCE_RECORDS",
        # Identifier detail intentionally presents retired projections as
        # evidence, including their is_active state and provenance.
        "api/graph/queries/persons.py:GET_PERSON_IDENTIFIERS",
        "api/graph/queries/persons.py:COUNT_PERSON_IDENTIFIERS",
    }
)

_AUTHORITATIVE_READERS: Final[frozenset[str]] = frozenset(
    """api/graph/queries/crm.py:GET_PERSON_CRM_METRICS
api/graph/queries/crm.py:_daily_buckets
api/graph/queries/entities.py:LIST_ENTITIES
api/graph/queries/entities.py:LIST_FILTER_SOURCE_SYSTEMS
api/graph/queries/entities.py:_CONNECTION_COUNT
api/graph/queries/entities.py:_ENTITY_PERSONS_HEAD
api/graph/queries/entities.py:_PHONE_CONFIDENCE
api/graph/queries/entities.py:_SOURCE_RECORD_COUNT
api/graph/queries/persons.py:COUNT_PERSON_CONNECTIONS_ADDRESS
api/graph/queries/persons.py:COUNT_PERSON_CONNECTIONS_ALL
api/graph/queries/persons.py:COUNT_PERSON_CONNECTIONS_IDENTIFIER
api/graph/queries/persons.py:COUNT_PERSON_CONNECTIONS_KNOWS
api/graph/queries/persons.py:COUNT_PERSON_SHARED_IDENTIFIERS
api/graph/queries/persons.py:COUNT_PERSON_SOURCE_RECORDS
api/graph/queries/persons.py:COUNT_PERSON_TIMELINE
api/graph/queries/persons.py:FIND_PERSON_BY_IDENTIFIER
api/graph/queries/persons.py:GET_PERSON_BY_ID
api/graph/queries/persons.py:GET_PERSON_CONNECTIONS_ADDRESS
api/graph/queries/persons.py:GET_PERSON_CONNECTIONS_ALL
api/graph/queries/persons.py:GET_PERSON_CONNECTIONS_IDENTIFIER
api/graph/queries/persons.py:GET_PERSON_CONNECTIONS_KNOWS
api/graph/queries/persons.py:GET_PERSON_ENTITIES
api/graph/queries/persons.py:GET_PERSON_LOYALTY
api/graph/queries/persons.py:GET_PERSON_POSSIBLE_MATCH_DETAIL
api/graph/queries/persons.py:GET_PERSON_SHARED_IDENTIFIERS
api/graph/queries/persons.py:GET_PERSON_SOURCE_RECORDS
api/graph/queries/persons.py:GET_PERSON_SOURCE_RECORD_ENTITY_FACETS
api/graph/queries/persons.py:GET_PERSON_TIMELINE
api/graph/queries/persons.py:GET_PERSON_TIMELINE_TARGET
api/graph/queries/persons.py:GET_PERSON_VEHICLES
api/graph/queries/persons.py:SEARCH_PERSONS
api/graph/queries/persons_list.py:GET_PERSON_LIST_SUMMARY
api/graph/queries/persons_list.py:_CONNECTION_COUNT
api/graph/queries/persons_list.py:_ENTITY_COUNT
api/graph/queries/persons_list.py:_ENTITY_ENRICHMENT
api/graph/queries/persons_list.py:_PHONE_CONFIDENCE
api/graph/queries/persons_list.py:_POSSIBLE_MATCH_COUNT
api/graph/queries/persons_list.py:_head
api/graph/queries/persons_list_filters.py:build_common_filter_clause
api/graph/queries/persons_list_filters.py:build_entity_filter_clause
api/graph/queries/reports.py:SEED_REPORTS
api/graph/queries/sales.py:COUNT_PERSON_SALES
api/graph/queries/sales.py:GET_PERSON_SALES
api/graph/queries/sales_prediction_discovery.py:DISCOVERY_DEAL_RECORDS
api/graph/queries/sales_prediction_gate.py:GATE_DEAL_VERSIONS_FOR_PARENTS
api/graph/queries/survivorship.py:CHECK_SOURCE_RECORD_LINKED
api/graph/queries/survivorship.py:GET_ADDRESS_FOR_SR
api/graph/queries/survivorship.py:GET_BEST_ADDRESS
api/graph/queries/survivorship.py:GET_BEST_IDENTIFIER
api/graph/queries/survivorship.py:GET_FACT_VALUE
api/graph/queries/survivorship.py:GET_FIELD_OPTIONS
api/graph/queries/survivorship.py:GET_IDENTIFIER_VALUE_FOR_SR
api/graph/queries/survivorship.py:GET_PERSON_FACTS
api/graph/queries/users.py:GET_ENTITIES_FOR_REVIEW_CASE
api/profile_analysis_runtime_queries.py:FETCH_PROFILE_ANALYSIS_SENSITIVE_VALUES
api/profile_analysis_runtime_queries.py:FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS
ingestion/graph/queries/crm_deal_count.py:_AUTHORITY_MATCH
ingestion/graph/queries/identity_link_revision_migrations.py:LIST_IDENTITY_LINK_BASELINE_BATCH
ingestion/graph/queries/knows.py:RESOLVE_KNOWS_ENDPOINTS
ingestion/graph/queries/knows.py:RESOLVE_PERSON_FROM_SOURCE_RECORD_ID
ingestion/graph/queries/knows.py:RESOLVE_PERSON_FROM_SOURCE_RECORD_PK
ingestion/graph/queries/knows.py:SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS
ingestion/graph/queries/knows.py:SCAN_CONTACT_SOURCE_RECORDS
ingestion/graph/queries/matching.py:CHECK_IDENTIFIER_FANOUT
ingestion/graph/queries/matching.py:FIND_CANDIDATES_BY_ADDRESS
ingestion/graph/queries/matching.py:FIND_CANDIDATES_BY_ADDRESSES_BATCH
ingestion/graph/queries/matching.py:FIND_CANDIDATES_BY_IDENTIFIER
ingestion/graph/queries/matching.py:FIND_CANDIDATES_BY_IDENTIFIERS_BATCH
ingestion/graph/queries/person_pairs.py:FIND_PERSONS_SHARING_IDENTIFIER
ingestion/graph/queries/person_pairs.py:FIND_PERSONS_SHARING_IDENTIFIERS_BATCH
ingestion/graph/queries/persons.py:FETCH_PERSON_ADDRESSES
ingestion/graph/queries/persons.py:FETCH_PERSON_FACTS
ingestion/graph/queries/persons.py:FETCH_PERSON_IDENTIFIERS
ingestion/graph/queries/persons.py:FETCH_PERSON_MATCH_ADDRESSES
ingestion/graph/queries/persons.py:FETCH_PERSON_MATCH_FACTS
ingestion/graph/queries/persons.py:FETCH_PERSON_MATCH_IDENTIFIERS
ingestion/graph/queries/pair_audit_recalc.py:READ_PAIR_AUDIT_BRIDGE
ingestion/graph/queries/persons.py:FETCH_ACTIVE_PERSON_AUTHORITY_WITH_OVERRIDES
ingestion/graph/queries/profile_analysis_dirty.py:FIND_PROFILE_ANALYSIS_MERGE_AFFECTED_PERSON_IDS
ingestion/graph/queries/sales.py:FIND_VEHICLE_CANDIDATES_FOR_SALES
ingestion/graph/queries/sales.py:RESOLVE_SALES_CUSTOMER
ingestion/graph/queries/sales_prediction.py:SALES_PREDICTION_DEAL_VERSIONS_FOR_PARENTS
ingestion/matching/deterministic.py:_FIND_ACTIVE_NO_MATCH_LOCKS
ingestion/matching/deterministic.py:_PERSON_HAS_CONFLICTING_GOVT_ID
ingestion/matching/deterministic.py:_PERSON_HAS_IDENTIFIER
ingestion/matching/deterministic.py:_PERSON_HAS_VALID_GOVT_ID""".splitlines()
)


def approved_reader_sources(repository_root: Path) -> tuple[Path, ...]:
    """Return every executable API/ingestion Python source, in stable order."""
    api = repository_root / "services" / "api" / "src"
    ingestion = repository_root / "services" / "ingestion" / "src"
    if not api.is_dir() or not ingestion.is_dir():
        raise RuntimeError(f"unresolvable repository reader root: {repository_root}")
    return tuple(sorted((*api.rglob("*.py"), *ingestion.rglob("*.py"))))


@dataclass(frozen=True)
class RelationshipReader:
    """One statically resolvable query binding that reads repairable links."""

    module: str
    symbol: str
    classification: ReaderClass
    query: str

    @property
    def identifier(self) -> str:
        """Return the stable, repository-relative classification key."""
        return f"{self.module}:{self.symbol}"


def discover_relationship_readers(*roots: Path) -> tuple[RelationshipReader, ...]:
    """Discover all statically resolvable relationship read-query bindings.

    ``roots`` may be query directories or individual Python modules.  Query
    builders are represented by their function name.  Dynamic construction that
    contains a relationship reader but cannot be resolved from source fails
    immediately instead of silently escaping the audit.
    """
    candidates: list[RelationshipReader] = []
    for file_path in _iter_python_files(roots):
        module = _module_name(file_path)
        source = file_path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(file_path))
        except SyntaxError as exc:
            raise RuntimeError(f"unresolvable query module: {file_path}") from exc
        for symbol, query in _query_bindings(tree, source, file_path):
            if not _is_relationship_read(query):
                continue
            identifier = f"{module}:{symbol}"
            if identifier in _AUDIT_READERS:
                classification: ReaderClass = "audit"
            elif identifier in _AUTHORITATIVE_READERS:
                classification = "authoritative"
            else:
                classification = "authoritative"
            candidates.append(RelationshipReader(module, symbol, classification, query))
    if not candidates:
        raise RuntimeError("no executable relationship reader candidates were discovered")
    return tuple(sorted(candidates, key=lambda item: item.identifier))


def assert_reader_contract(*roots: Path) -> tuple[RelationshipReader, ...]:
    """Fail closed on unknown readers or authoritative inactive-link reads."""
    readers = discover_relationship_readers(*roots)
    identifiers = {reader.identifier for reader in readers}
    unclassified = identifiers - _AUDIT_READERS - _AUTHORITATIVE_READERS
    stale_classifications = (_AUDIT_READERS | _AUTHORITATIVE_READERS) - identifiers
    if unclassified:
        raise RuntimeError("unclassified relationship reader: " + ", ".join(sorted(unclassified)))
    if stale_classifications:
        raise RuntimeError(
            "reader classification no longer resolves: " + ", ".join(sorted(stale_classifications))
        )
    for reader in readers:
        if reader.classification == "authoritative" and not _has_active_predicate(reader):
            raise RuntimeError(
                f"authoritative relationship reader lacks active predicate: {reader.identifier}"
            )
    return readers


def _iter_python_files(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    files: set[Path] = set()
    for root in roots:
        if root.is_file() and root.suffix == ".py":
            files.add(root)
        elif root.is_dir():
            if (root / "services" / "api" / "src").is_dir():
                files.update(approved_reader_sources(root))
            else:
                files.update(path for path in root.rglob("*.py") if path.name != "__init__.py")
        else:
            raise RuntimeError(f"unresolvable reader source root: {root}")
    return tuple(sorted(files))


def _module_name(file_path: Path) -> str:
    parts = file_path.parts
    try:
        services_index = parts.index("services")
        service = parts[services_index + 1]
        source_index = parts.index("src", services_index)
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"reader source is outside a services/*/src tree: {file_path}") from exc
    return f"{service}/" + "/".join(parts[source_index + 1 :])


def _query_bindings(tree: ast.Module, source: str, file_path: Path) -> tuple[tuple[str, str], ...]:
    bindings: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            symbol = _assignment_name(node)
            value = node.value
            if symbol is not None and value is not None:
                bindings.append((symbol, _source_segment(source, value, file_path)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bindings.append((node.name, _source_segment(source, node, file_path)))
    return tuple(bindings)


def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    if isinstance(node, ast.Assign):
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            return None
        return node.targets[0].id
    return node.target.id if isinstance(node.target, ast.Name) else None


def _source_segment(source: str, node: ast.AST, file_path: Path) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        raise RuntimeError(f"unresolvable executable reader candidate: {file_path}")
    return segment


def _is_relationship_read(query: str) -> bool:
    return bool(
        _RELATIONSHIP_PATTERN.search(query)
        and _READ_PATTERN.search(query)
        and not _WRITE_PATTERN.search(query)
    )


def _has_active_predicate(reader: RelationshipReader) -> bool:
    """Allow literal predicates and named literal policy fragments."""
    if _ACTIVE_PATTERN.search(reader.query):
        return True
    return "_LINK_ACTIVE" in reader.query
