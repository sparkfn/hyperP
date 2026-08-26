"""CI-compatible Neo4j parser coverage for identity-link stream Cypher.

The existing disposable person-list Neo4j service is reused without writes; this
only EXPLAINs the stream queries so CI validates their Neo4j 5 syntax.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from neo4j import GraphDatabase
from src.graph.queries.identity_link_revisions import (
    APPEND_IDENTITY_LINK_REVISIONS,
    GET_AFFECTED_IDENTITY_LINK_HEADS,
    LIST_IDENTITY_LINK_EVENTS,
    LIST_IDENTITY_LINK_SNAPSHOT,
)


def test_identity_link_queries_explain_on_disposable_ci_neo4j() -> None:
    uri = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_URI")
    if uri is None:
        pytest.skip("disposable CI Neo4j query-check database is not configured")
    host = urlparse(uri).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    service_host = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_SERVICE_HOST")
    if service_host is not None:
        allowed_hosts.add(service_host)
    if host not in allowed_hosts:
        pytest.fail("identity-link query checks only accept the disposable CI Neo4j host")
    password = os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD")
    if password is None:
        pytest.fail("HYPERP_NEO4J_PERSON_LIST_TEST_PASSWORD is required")

    driver = GraphDatabase.driver(
        uri,
        auth=(os.getenv("HYPERP_NEO4J_PERSON_LIST_TEST_USER", "neo4j"), password),
        connection_timeout=5,
    )
    try:
        with driver.session() as session:
            session.run(
                "EXPLAIN\n" + APPEND_IDENTITY_LINK_REVISIONS,
                rows=[
                    {
                        "link_key": (
                            "ilk1:11:bitrix_chat8:portal-17:contact9:contact-1"
                            "24:crm_contact_identity_v1"
                        ),
                        "cause_key": "query-check",
                        "source_system": "bitrix_chat",
                        "source_instance_id": "portal-1",
                        "source_entity_type": "contact",
                        "source_entity_id": "contact-1",
                        "identity_policy_version": "crm_contact_identity_v1",
                        "link_status": "unresolved",
                        "hyperp_person_id": None,
                        "resolution_kind": "baseline",
                        "effective_at": "2026-08-26T00:00:00+00:00",
                        "match_decision_id": None,
                        "review_case_id": None,
                    }
                ],
                skip_existing_heads=False,
            ).consume()
            session.run(
                "EXPLAIN\n" + GET_AFFECTED_IDENTITY_LINK_HEADS,
                merge_event_id="merge-1",
                absorbed_person_id="person-absorbed",
                operation="merge",
                merge_cause_prefix="person-merge:merge-1:",
            ).consume()
            session.run(
                "EXPLAIN\n" + LIST_IDENTITY_LINK_EVENTS,
                after_revision=0,
                through_revision=1,
                limit=50,
            ).consume()
            session.run(
                "EXPLAIN\n" + LIST_IDENTITY_LINK_SNAPSHOT,
                snapshot_revision=1,
                after_link_key="",
                limit=51,
            ).consume()
    finally:
        driver.close()
