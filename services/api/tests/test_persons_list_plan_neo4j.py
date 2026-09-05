"""Neo4j 5.26 planner regression coverage for the default Person list."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from time import monotonic, sleep
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from neo4j import Driver, GraphDatabase, Session
from neo4j.exceptions import ServiceUnavailable
from src.graph.queries.indexes import (
    PERSON_COMPLETENESS_INDEX_CYPHER,
    PERSON_COMPLETENESS_INDEX_LABEL,
    PERSON_COMPLETENESS_INDEX_NAME,
    PERSON_COMPLETENESS_INDEX_PROPERTY,
    PERSON_CRM_DEAL_COUNT_INDEX_CYPHER,
    PERSON_CRM_DEAL_COUNT_INDEX_NAME,
    PERSON_CRM_DEAL_COUNT_INDEX_PROPERTY,
    PERSON_INDEXES,
    build_person_completeness_index_cypher,
    build_person_crm_deal_count_index_cypher,
)
from src.graph.queries.persons_list import build_count_persons_query, build_list_persons_query

_SCHEMA_MUTATION_OPT_IN = "HYPERP_NEO4J_PERSON_LIST_TEST_ALLOW_SCHEMA_MUTATION"
_TEST_INDEX_PREFIXES = {
    "completeness": "person_list_test_completeness_",
    "crm_deal_count": "person_list_test_crm_deal_count_",
}
_INDEX_ACCESS_OPERATORS = frozenset(
    {"NodeIndexScan", "PartitionedNodeIndexScan", "NodeIndexSeek", "NodeIndexSeekByRange"}
)
_LABEL_SCAN_OPERATORS = frozenset({"NodeByLabelScan", "PartitionedNodeByLabelScan"})
_OUTER_PERSON_BINDING = re.compile(r"(?<![A-Za-z0-9_`])`?p`?\s*:\s*`?Person`?(?![A-Za-z0-9_`])")


@dataclass(frozen=True)
class _PlannerGraph:
    driver: Driver
    run_id: str

    def index_name(self, kind: str) -> str:
        return _safe_index_name(self.run_id, kind=kind)


@dataclass(frozen=True)
class _IndexMetadata:
    name: str
    index_type: str
    entity_type: str
    labels_or_types: tuple[str, ...]
    properties: tuple[str, ...]
    state: str
    failure_message: str


@dataclass(frozen=True)
class _PlanNode:
    operator_type: str
    identifiers: tuple[str, ...]
    details: str


def _verify_connectivity(driver: Driver) -> None:
    deadline = monotonic() + 60
    while True:
        try:
            driver.verify_connectivity()
            return
        except ServiceUnavailable:
            if monotonic() >= deadline:
                raise
            sleep(1)


def _validate_planner_target(uri: str) -> None:
    parsed = urlparse(uri)
    service_host = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_SERVICE_HOST")
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    configured_ci_service = (
        service_host is not None
        and service_host != ""
        and parsed.hostname == service_host
        and parsed.port == 7687
    )
    if parsed.scheme != "bolt":
        pytest.fail("Planner regression test requires a direct Bolt URI")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("Planner regression test URI must not contain user information")
    if not loopback and not configured_ci_service:
        pytest.fail("Planner schema mutation is restricted to loopback or its configured CI Neo4j")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        pytest.fail("Planner regression test URI must not select a database or include options")


def _safe_index_name(run_id: str, *, kind: str) -> str:
    prefix = _TEST_INDEX_PREFIXES.get(kind)
    if prefix is None:
        raise ValueError("Planner test index kind is not allowed")
    name = f"{prefix}{run_id}"
    if re.fullmatch(r"person_list_test_(?:completeness|crm_deal_count)_[0-9a-f]+", name) is None:
        raise ValueError("Planner test generated an unsafe schema identifier")
    return name


@pytest.fixture
def planner_graph() -> Iterator[_PlannerGraph]:
    if os.getenv(_SCHEMA_MUTATION_OPT_IN) != "1":
        pytest.skip(f"{_SCHEMA_MUTATION_OPT_IN}=1 is required for the planner regression test")
    uri = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_URI")
    if uri is None:
        pytest.skip("disposable person-list Neo4j test database is not configured")
    _validate_planner_target(uri)
    password = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD is required")
    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_USER", "neo4j"), password),
        connection_timeout=5,
    )
    try:
        _verify_connectivity(driver)
        run_id = uuid4().hex
        graph = _PlannerGraph(driver=driver, run_id=run_id)
        try:
            yield graph
        finally:
            _cleanup_planner_graph(graph)
    finally:
        driver.close()


def _cleanup_planner_graph(graph: _PlannerGraph) -> None:
    actions = (
        _drop_owned_index,
        _delete_owned_nodes,
        _assert_owned_index_removed,
        _assert_owned_nodes_removed,
    )
    errors: list[Exception] = []
    for action in actions:
        try:
            action(graph)
        except Exception as exc:  # noqa: BLE001 - attempt every independent cleanup action
            errors.append(exc)
    if errors:
        raise ExceptionGroup("Planner graph cleanup failed", errors)


def _drop_owned_index(graph: _PlannerGraph) -> None:
    with graph.driver.session() as session:
        for kind in _TEST_INDEX_PREFIXES:
            index_name = graph.index_name(kind)
            session.run(f"DROP INDEX `{index_name}` IF EXISTS").consume()


def _delete_owned_nodes(graph: _PlannerGraph) -> None:
    with graph.driver.session() as session:
        session.run(
            "MATCH (node {_person_list_test_run: $run_id}) DETACH DELETE node",
            run_id=graph.run_id,
        ).consume()


def _assert_owned_index_removed(graph: _PlannerGraph) -> None:
    with graph.driver.session() as session:
        for kind in _TEST_INDEX_PREFIXES:
            assert _get_index_metadata(session, graph.index_name(kind)) is None


def _assert_owned_nodes_removed(graph: _PlannerGraph) -> None:
    with graph.driver.session() as session:
        remaining = session.run(
            """
            MATCH (node {_person_list_test_run: $run_id})
            RETURN count(node) AS total
            """,
            run_id=graph.run_id,
        ).single(strict=True)["total"]
        assert remaining == 0


def _to_string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Neo4j plan/index field {field!r} must be a string sequence")
    return tuple(value)


def _as_string_mapping(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Neo4j plan field {field!r} must be a mapping")
    parsed: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"Neo4j plan field {field!r} contains a non-string key")
        parsed[key] = item
    return parsed


def parse_plan_nodes(raw_plan: object) -> list[_PlanNode]:
    pending = [_as_string_mapping(raw_plan, field="plan")]
    nodes: list[_PlanNode] = []
    while pending:
        raw_node = pending.pop()
        operator_type = raw_node.get("operatorType")
        if not isinstance(operator_type, str):
            raise ValueError("Neo4j plan node must contain a string operatorType")
        identifiers = _to_string_tuple(raw_node.get("identifiers"), field="identifiers")
        arguments = _as_string_mapping(raw_node.get("args"), field="args")
        details = arguments.get("Details", arguments.get("details", ""))
        if not isinstance(details, str):
            raise ValueError("Neo4j plan Details argument must be a string")
        raw_children = raw_node.get("children", ())
        if not isinstance(raw_children, (list, tuple)):
            raise ValueError("Neo4j plan children must be a sequence")
        pending.extend(_as_string_mapping(child, field="child") for child in raw_children)
        nodes.append(
            _PlanNode(
                operator_type=operator_type.partition("@")[0],
                identifiers=identifiers,
                details=details,
            )
        )
    return nodes


def _is_outer_person_access(node: _PlanNode) -> bool:
    return "p" in node.identifiers and _OUTER_PERSON_BINDING.search(node.details) is not None


def _get_index_metadata(session: Session, index_name: str) -> _IndexMetadata | None:
    record = session.run(
        """
        SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state,
          failureMessage
        WHERE name = $index_name
        RETURN name, type, entityType, labelsOrTypes, properties, state, failureMessage
        """,
        index_name=index_name,
    ).single()
    if record is None:
        return None
    return _IndexMetadata(
        name=str(record["name"]),
        index_type=str(record["type"]),
        entity_type=str(record["entityType"]),
        labels_or_types=_to_string_tuple(record["labelsOrTypes"], field="labelsOrTypes"),
        properties=_to_string_tuple(record["properties"], field="properties"),
        state=str(record["state"]),
        failure_message=str(record["failureMessage"] or ""),
    )


def _assert_pristine_database(session: Session) -> None:
    total = session.run("MATCH (node) RETURN count(node) AS total").single(strict=True)["total"]
    if total != 0:
        pytest.fail("Planner regression test requires an empty disposable Neo4j database")
    user_indexes = [
        dict(record) for record in session.run("SHOW INDEXES YIELD *") if record["type"] != "LOOKUP"
    ]
    constraints = [dict(record) for record in session.run("SHOW CONSTRAINTS YIELD *")]
    if user_indexes or constraints:
        pytest.fail(
            f"Planner regression test requires pristine schema; indexes={user_indexes!r}, "
            f"constraints={constraints!r}"
        )


def _seed_planner_population(session: Session, run_id: str) -> None:
    # 10k+ rows make the pinned 5.26 planner choose between a population scan and
    # the ordered completeness range index under the issue's representative size.
    session.run(
        """
        UNWIND range(0, 10199) AS i
        CREATE (:Person {
          person_id: 'plan-person-' + right('00000' + toString(i), 5),
          status: CASE WHEN i = 0 THEN 'suppressed' ELSE 'active' END,
          profile_completeness_score: [0.0, 0.2, 0.4, 0.6, 0.8, 1.0][i % 6],
          crm_deal_count: CASE WHEN i % 10 = 0 THEN 1 + (i % 4) ELSE 0 END,
          _person_list_test_run: $run_id
        })
        """,
        run_id=run_id,
    ).consume()
    session.run(
        """
        UNWIND range(0, 19) AS i
        CREATE (:Person {
          person_id: 'missing-score-' + toString(i), status: 'active',
          _person_list_test_run: $run_id
        })
        CREATE (:Person {
          person_id: 'merged-score-' + toString(i), status: 'merged',
          profile_completeness_score: 1.0, _person_list_test_run: $run_id
        })
        """,
        run_id=run_id,
    ).consume()


def _wait_for_index_online(session: Session, index_name: str) -> _IndexMetadata:
    deadline = monotonic() + 60
    while monotonic() < deadline:
        metadata = _get_index_metadata(session, index_name)
        if metadata is not None and metadata.state == "FAILED":
            pytest.fail(f"Test index failed to populate: {metadata!r}")
        if metadata is not None and metadata.state == "ONLINE":
            return metadata
        sleep(0.1)
    pytest.fail(f"Timed out waiting for test-owned index {index_name!r} to become ONLINE")


def _assert_expected_index(
    metadata: _IndexMetadata,
    *,
    index_name: str,
    property_name: str,
) -> None:
    assert metadata == _IndexMetadata(
        name=index_name,
        index_type="RANGE",
        entity_type="NODE",
        labels_or_types=(PERSON_COMPLETENESS_INDEX_LABEL,),
        properties=(property_name,),
        state="ONLINE",
        failure_message="",
    )


def test_plan_parser_uses_exact_identifiers_and_nested_children() -> None:
    raw_plan = {
        "operatorType": "ProduceResults@neo4j",
        "args": {"Details": "person"},
        "identifiers": ["person"],
        "children": [
            {
                "operatorType": "NodeByLabelScan",
                "args": {"Details": "other:Person"},
                "identifiers": ["p", "other"],
                "children": [],
            },
            {
                "operatorType": "NodeIndexScan@neo4j",
                "args": {"Details": "RANGE INDEX p:Person(profile_completeness_score)"},
                "identifiers": ["p"],
            },
        ],
    }
    nodes = parse_plan_nodes(raw_plan)
    outer = [node for node in nodes if _is_outer_person_access(node)]
    assert [node.operator_type for node in outer] == ["NodeIndexScan"]


@pytest.mark.parametrize(
    "raw_plan",
    [
        {},
        {"operatorType": 42, "args": {}, "identifiers": [], "children": []},
        {"operatorType": "Scan", "args": [], "identifiers": [], "children": []},
        {"operatorType": "Scan", "args": {}, "identifiers": "p", "children": []},
        {"operatorType": "Scan", "args": {}, "identifiers": [], "children": {}},
    ],
)
def test_plan_parser_rejects_malformed_nodes(raw_plan: object) -> None:
    with pytest.raises(ValueError):
        parse_plan_nodes(raw_plan)


def test_production_completeness_index_definition_is_stable() -> None:
    assert PERSON_COMPLETENESS_INDEX_NAME == "idx_person_completeness"
    assert PERSON_COMPLETENESS_INDEX_CYPHER == (
        "CREATE INDEX idx_person_completeness IF NOT EXISTS "
        "FOR (p:Person) ON (p.profile_completeness_score)"
    )
    assert PERSON_INDEXES.count(PERSON_COMPLETENESS_INDEX_CYPHER) == 1


def test_completeness_index_builder_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError):
        build_person_completeness_index_cypher("unsafe index")


def test_crm_deal_count_index_definition_is_stable() -> None:
    assert PERSON_CRM_DEAL_COUNT_INDEX_NAME == "idx_person_crm_deal_count"
    assert PERSON_CRM_DEAL_COUNT_INDEX_CYPHER == (
        "CREATE INDEX idx_person_crm_deal_count IF NOT EXISTS FOR (p:Person) ON (p.crm_deal_count)"
    )
    assert PERSON_INDEXES.count(PERSON_CRM_DEAL_COUNT_INDEX_CYPHER) == 1


def test_crm_deal_count_index_builder_rejects_unsafe_names() -> None:
    with pytest.raises(ValueError):
        build_person_crm_deal_count_index_cypher("unsafe index")


def test_default_list_plan_uses_completeness_index(planner_graph: _PlannerGraph) -> None:
    with planner_graph.driver.session() as session:
        _assert_pristine_database(session)
        _seed_planner_population(session, planner_graph.run_id)
        index_name = planner_graph.index_name("completeness")
        session.run(build_person_completeness_index_cypher(index_name)).consume()
        metadata = _wait_for_index_online(session, index_name)
        _assert_expected_index(
            metadata,
            index_name=index_name,
            property_name=PERSON_COMPLETENESS_INDEX_PROPERTY,
        )
        raw_plan = (
            session.run(
                "EXPLAIN\n" + build_list_persons_query(None, None, has_q=False),
                skip=0,
                limit=26,
            )
            .consume()
            .plan
        )
    nodes = parse_plan_nodes(raw_plan)
    index_accesses = [
        node
        for node in nodes
        if node.operator_type in _INDEX_ACCESS_OPERATORS
        and _is_outer_person_access(node)
        and PERSON_COMPLETENESS_INDEX_PROPERTY in node.details
    ]
    label_scans = [
        node
        for node in nodes
        if node.operator_type in _LABEL_SCAN_OPERATORS and _is_outer_person_access(node)
    ]
    assert index_accesses, f"Expected outer p index access; plan={nodes!r}, index={metadata!r}"
    assert not label_scans, f"Unexpected outer p label scan; plan={nodes!r}"


@dataclass(frozen=True)
class _CrmPlanCase:
    name: str
    query: str
    parameters: dict[str, int]
    expected_operators: frozenset[str]


def _crm_plan_cases() -> tuple[_CrmPlanCase, ...]:
    equality = frozenset({"NodeIndexSeek", "NodeIndexSeekByRange"})
    range_seek = frozenset({"NodeIndexSeekByRange"})
    ordered_scan = _INDEX_ACCESS_OPERATORS
    zero_filters = frozenset({"crm_deal_count_min", "crm_deal_count_max"})
    has_deals = frozenset({"crm_deal_count_min"})
    closed_range = frozenset({"crm_deal_count_min", "crm_deal_count_max"})
    return (
        _CrmPlanCase(
            "sort-asc-list",
            build_list_persons_query("crm_deal_count", "asc", has_q=False),
            {"skip": 0, "limit": 26},
            ordered_scan,
        ),
        _CrmPlanCase(
            "sort-asc-count",
            build_count_persons_query("crm_deal_count", "asc", has_q=False),
            {},
            ordered_scan,
        ),
        _CrmPlanCase(
            "sort-desc-list",
            build_list_persons_query("crm_deal_count", "desc", has_q=False),
            {"skip": 0, "limit": 26},
            ordered_scan,
        ),
        _CrmPlanCase(
            "sort-desc-count",
            build_count_persons_query("crm_deal_count", "desc", has_q=False),
            {},
            ordered_scan,
        ),
        _CrmPlanCase(
            "zero-list",
            build_list_persons_query(
                "preferred_full_name", "asc", has_q=False, active_filters=zero_filters
            ),
            {"skip": 0, "limit": 26, "crm_deal_count_min": 0, "crm_deal_count_max": 0},
            equality,
        ),
        _CrmPlanCase(
            "zero-count",
            build_count_persons_query(
                "preferred_full_name", "asc", has_q=False, active_filters=zero_filters
            ),
            {"crm_deal_count_min": 0, "crm_deal_count_max": 0},
            equality,
        ),
        _CrmPlanCase(
            "has-deals-list",
            build_list_persons_query(
                "preferred_full_name", "asc", has_q=False, active_filters=has_deals
            ),
            {"skip": 0, "limit": 26, "crm_deal_count_min": 1},
            range_seek,
        ),
        _CrmPlanCase(
            "has-deals-count",
            build_count_persons_query(
                "preferred_full_name", "asc", has_q=False, active_filters=has_deals
            ),
            {"crm_deal_count_min": 1},
            range_seek,
        ),
        _CrmPlanCase(
            "closed-range-list",
            build_list_persons_query(
                "preferred_full_name", "asc", has_q=False, active_filters=closed_range
            ),
            {"skip": 0, "limit": 26, "crm_deal_count_min": 1, "crm_deal_count_max": 2},
            range_seek,
        ),
        _CrmPlanCase(
            "closed-range-count",
            build_count_persons_query(
                "preferred_full_name", "asc", has_q=False, active_filters=closed_range
            ),
            {"crm_deal_count_min": 1, "crm_deal_count_max": 2},
            range_seek,
        ),
    )


def _assert_crm_plan(case: _CrmPlanCase, raw_plan: object) -> None:
    nodes = parse_plan_nodes(raw_plan)
    index_accesses = [
        node
        for node in nodes
        if node.operator_type in case.expected_operators
        and _is_outer_person_access(node)
        and PERSON_CRM_DEAL_COUNT_INDEX_PROPERTY in node.details
    ]
    label_scans = [
        node
        for node in nodes
        if node.operator_type in _LABEL_SCAN_OPERATORS and _is_outer_person_access(node)
    ]
    plan_details = "\n".join(node.details.lower() for node in nodes)
    assert index_accesses, f"{case.name}: expected CRM count index access; plan={nodes!r}"
    assert not label_scans, f"{case.name}: unexpected outer p label scan; plan={nodes!r}"
    assert re.search(r"\bcrm_deal\b", plan_details) is None
    assert "bitrix_chat" not in plan_details


def test_plan_parser_detects_outer_person_label_scan() -> None:
    raw_plan = {
        "operatorType": "NodeByLabelScan",
        "args": {"Details": "p:Person"},
        "identifiers": ["p"],
        "children": [],
    }
    nodes = parse_plan_nodes(raw_plan)
    assert len(nodes) == 1
    assert _is_outer_person_access(nodes[0]) is True


def test_crm_deal_count_list_and_count_plans_use_range_index(
    planner_graph: _PlannerGraph,
) -> None:
    cases = _crm_plan_cases()
    with planner_graph.driver.session() as session:
        _assert_pristine_database(session)
        _seed_planner_population(session, planner_graph.run_id)
        index_name = planner_graph.index_name("crm_deal_count")
        session.run(build_person_crm_deal_count_index_cypher(index_name)).consume()
        metadata = _wait_for_index_online(session, index_name)
        _assert_expected_index(
            metadata,
            index_name=index_name,
            property_name=PERSON_CRM_DEAL_COUNT_INDEX_PROPERTY,
        )
        raw_plans = [
            session.run("EXPLAIN\n" + case.query, **case.parameters).consume().plan
            for case in cases
        ]

    for case, raw_plan in zip(cases, raw_plans, strict=True):
        _assert_crm_plan(case, raw_plan)
