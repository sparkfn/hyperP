"""Startup ordering tests for lifecycle data repair and deferred constraints."""

from __future__ import annotations

from typing import cast

import pytest
from src import main
from src.config import Settings
from src.graph.schema_init import (
    BASE_LIFECYCLE_CONSTRAINTS,
    DEFERRED_SOURCE_RECORD_CONSTRAINTS,
    _find_init_cypher,
    _split_statements,
)


class _Client:
    def verify_connectivity(self) -> None:
        pass

    def close(self) -> None:
        pass


def test_version_key_constraint_is_deferred_from_base_schema() -> None:
    base = "\n".join(BASE_LIFECYCLE_CONSTRAINTS)
    deferred = "\n".join(DEFERRED_SOURCE_RECORD_CONSTRAINTS)
    assert "source_record_version_key_unique" not in base
    assert "source_record_version_key_unique" in deferred


def test_profile_analysis_schema_statements_are_idempotent() -> None:
    statements = _split_statements(_find_init_cypher().read_text(encoding="utf-8"))
    profile_analysis_statements = [
        statement for statement in statements if "ProfileAnalysis" in statement
    ]
    assert profile_analysis_statements == [
        """CREATE CONSTRAINT profile_analysis_id_unique IF NOT EXISTS
  FOR (pa:ProfileAnalysis) REQUIRE pa.analysis_id IS UNIQUE""",
        """CREATE INDEX idx_profile_analysis_history IF NOT EXISTS
  FOR (pa:ProfileAnalysis)
  ON (pa.person_id, pa.analysis_type, pa.completed_at)""",
    ]


def test_canonical_schema_contains_person_and_knows_performance_indexes() -> None:
    statements = _split_statements(_find_init_cypher().read_text(encoding="utf-8"))
    schema = "\n".join(statements)

    for name in (
        "idx_person_completeness",
        "idx_person_high_value",
        "idx_person_high_risk",
        "idx_person_updated_at",
        "idx_knows_source_record_pk",
    ):
        assert name in schema
    assert "FOR ()-[r:KNOWS]-() ON (r.source_record_pk)" in schema

def test_lifecycle_repair_precedes_source_version_uniqueness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    client = _Client()

    monkeypatch.setattr(main, "get_settings", lambda: cast(Settings, object()))
    monkeypatch.setattr(main, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(main, "apply_schema", lambda _client: calls.append("base_schema"))
    monkeypatch.setattr(
        main, "bootstrap_entities_and_sources", lambda _client: calls.append("bootstrap")
    )
    monkeypatch.setattr(
        main, "apply_data_migrations", lambda _client: calls.append("lifecycle_repair")
    )
    monkeypatch.setattr(
        main,
        "apply_deferred_source_record_constraints",
        lambda _client: calls.append("source_version_constraint"),
        raising=False,
    )

    main.initialize_ingestion_graph()

    assert calls == [
        "base_schema",
        "bootstrap",
        "lifecycle_repair",
        "source_version_constraint",
    ]
