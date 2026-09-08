"""Read-only query and parameterisation contract checks."""

from __future__ import annotations

import re

from intelligence.graph.queries import crm_deal_refs as queries
from intelligence.repositories.neo4j.crm_deal_refs import Neo4jCrmDealRefsRepository


def test_queries_are_read_only_and_source_scoped() -> None:
    query_text = "\n".join(
        (
            queries.VALIDATE_SOURCE_INSTANCE,
            queries.READ_IDENTITY_BOUNDARY,
            queries.LIST_DEAL_REFERENCE_PAGE,
            queries.LIST_IDENTITY_REVISION_PAGE,
        )
    ).upper()
    for forbidden in ("CREATE", "MERGE", "SET", "DELETE", "REMOVE"):
        assert re.search(rf"\b{forbidden}\b", query_text) is None
    assert "$source_instance_id" in queries.LIST_DEAL_REFERENCE_PAGE
    assert "identity_policy_version" in queries.LIST_DEAL_REFERENCE_PAGE
    assert "record_type: 'crm_deal'" in queries.LIST_DEAL_REFERENCE_PAGE
    assert "source_key: 'bitrix_chat'" in queries.LIST_DEAL_REFERENCE_PAGE
    assert "ORDER BY record.source_record_id" in queries.LIST_DEAL_REFERENCE_PAGE
    assert "global_revision <= $through_revision" in queries.LIST_IDENTITY_REVISION_PAGE
    assert "counter.baseline_completed_at" in queries.READ_IDENTITY_BOUNDARY
    assert "migration.completed_at" in queries.READ_IDENTITY_BOUNDARY
    assert "owned_entity_keys" in queries.LIST_DEAL_REFERENCE_PAGE
    assert "record_entity_key" in queries.LIST_DEAL_REFERENCE_PAGE


def test_repository_executes_source_validation_in_read_mode_with_parameters() -> None:
    captured: dict[str, object] = {}

    class Session:
        def __enter__(self) -> Session:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def run(self, query: str, parameters: dict[str, object]) -> list[dict[str, object]]:
            captured["query"] = query
            captured["parameters"] = parameters
            return [{"source_instance_id": "instance-a"}]

    class Driver:
        def session(self, **kwargs: object) -> Session:
            captured["session"] = kwargs
            return Session()

        def close(self) -> None:
            return None

    repository = Neo4jCrmDealRefsRepository(Driver(), "neo4j")  # type: ignore[arg-type]
    repository.validate_source_instance("instance-a")
    assert captured["session"] == {"database": "neo4j", "default_access_mode": "READ"}
    assert captured["query"] == queries.VALIDATE_SOURCE_INSTANCE
    assert captured["parameters"] == {"source_instance_id": "instance-a"}
