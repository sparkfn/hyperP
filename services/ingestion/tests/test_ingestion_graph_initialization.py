"""Graph initialization ordering tests for migrations and deferred constraints."""

from __future__ import annotations

from typing import cast

import pytest
from src import main
from src.config import Settings
from src.graph import migrations
from src.graph.client import Neo4jClient
from src.graph.queries.stage_history_ingestion import (
    CREATE_STAGE_HISTORY_INGESTION_CONSTRAINTS,
)
from src.graph.schema_init import (
    BASE_LIFECYCLE_CONSTRAINTS,
    DEFERRED_SOURCE_RECORD_CONSTRAINTS,
    _find_init_cypher,
    _split_statements,
)
from src.ingestion_config import BitrixOpenLinesConfig, IngestionConfig


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


def test_retired_global_incremental_checkpoint_constraint_is_not_recreated() -> None:
    schema = _find_init_cypher().read_text(encoding="utf-8")
    assert "ingestion_checkpoint_key_unique" not in schema


def test_control_binding_schema_is_available_in_runtime_and_canonical_initialization() -> None:
    canonical_schema = _find_init_cypher().read_text(encoding="utf-8")
    runtime_schema = "\n".join(BASE_LIFECYCLE_CONSTRAINTS)
    identity = "(binding.source_key, binding.control_instance_id)"

    assert "bitrix_execution_source_binding_control_unique" in canonical_schema
    assert identity in canonical_schema
    assert "bitrix_execution_source_binding_control_unique" in runtime_schema
    assert identity in runtime_schema


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
        "idx_person_crm_deal_count",
        "idx_person_high_value",
        "idx_person_high_risk",
        "idx_person_updated_at",
        "idx_knows_source_record_pk",
        "idx_identified_by_source_record_pk",
        "idx_lives_at_source_record_pk",
        "idx_has_fact_source_record_pk",
        "idx_purchased_source_record_pk",
        "idx_bought_vehicle_source_record_pk",
        "idx_owns_vehicle_source_record_pk",
    ):
        assert name in schema
    assert "FOR ()-[r:KNOWS]-() ON (r.source_record_pk)" in schema
    for relationship_type in (
        "IDENTIFIED_BY",
        "LIVES_AT",
        "HAS_FACT",
        "PURCHASED",
        "BOUGHT_VEHICLE",
        "OWNS_VEHICLE",
    ):
        assert f"FOR ()-[r:{relationship_type}]-() ON (r.source_record_pk)" in schema


def test_stage_history_schema_is_available_in_both_initialization_paths() -> None:
    def normalized(statements: tuple[str, ...] | list[str]) -> set[str]:
        return {" ".join(statement.split()) for statement in statements}

    dynamic_schema = normalized(CREATE_STAGE_HISTORY_INGESTION_CONSTRAINTS)
    base_schema = normalized(BASE_LIFECYCLE_CONSTRAINTS)
    canonical_schema = normalized(
        _split_statements(_find_init_cypher().read_text(encoding="utf-8"))
    )

    assert dynamic_schema <= base_schema
    assert dynamic_schema <= canonical_schema


def test_data_migrations_precede_source_version_uniqueness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    migration_options: list[dict[str, object]] = []
    client = _Client()

    monkeypatch.setattr(main, "get_settings", lambda: cast(Settings, object()))
    monkeypatch.setattr(
        main,
        "get_ingestion_config",
        lambda: IngestionConfig(
            bitrix_openlines=BitrixOpenLinesConfig(
                source_instance_id="bitrix-primary",
                included_crm_category_ids=["2"],
                entity_by_crm_category_id={"2": "speedzone"},
            )
        ),
    )
    monkeypatch.setattr(main, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(main, "apply_schema", lambda _client: calls.append("base_schema"))
    monkeypatch.setattr(
        main, "bootstrap_entities_and_sources", lambda _client: calls.append("bootstrap")
    )
    monkeypatch.setattr(
        main,
        "bootstrap_legacy_bitrix_source_instance",
        lambda _client: calls.append("legacy_registry"),
    )

    def _migrate(_client: object, *, ensure_legacy_registration: object) -> None:
        calls.append("control_instance_migration")
        ensure_legacy_registration()

    monkeypatch.setattr(main, "migrate_ingestion_control_instances", _migrate)
    monkeypatch.setattr(
        main,
        "assert_ingestion_control_ready",
        lambda _client: calls.append("control_instance_ready"),
    )
    monkeypatch.setattr(
        main,
        "apply_data_migrations",
        lambda _client, **kwargs: (
            calls.append("data_migrations"),
            migration_options.append(kwargs),
        ),
    )
    monkeypatch.setattr(
        main,
        "apply_deferred_source_record_constraints",
        lambda _client: calls.append("source_version_constraint"),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "apply_deferred_identifier_scope_constraints",
        lambda _client: calls.append("identifier_scope_constraint"),
        raising=False,
    )

    main.initialize_ingestion_graph()

    assert calls == [
        "base_schema",
        "bootstrap",
        "control_instance_migration",
        "legacy_registry",
        "control_instance_ready",
        "data_migrations",
        "source_version_constraint",
        "identifier_scope_constraint",
    ]
    assert migration_options == [
        {
            "bitrix_source_instance_id": "bitrix-primary",
            "bitrix_crm_category_entities": {"2": "speedzone"},
            "included_bitrix_crm_category_ids": ["2"],
        },
    ]


def test_data_migrations_exclude_recurring_lifecycle_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    client = cast(Neo4jClient, _Client())

    for name in (
        "backfill_record_type_subtypes",
        "backfill_missing_person_completeness_scores",
        "migrate_bitrix_chat_source",
        "migrate_crm_deal_stage_projection",
        "migrate_fundbox_source_keys",
        "migrate_source_record_lifecycle",
        "migrate_source_record_source_instances",
        "migrate_identifier_scopes",
        "migrate_projection_relationship_lifecycle",
        "migrate_identity_link_revision_baseline",
    ):
        monkeypatch.setattr(
            migrations,
            name,
            lambda _client, *args, migration=name, **kwargs: calls.append(migration),
        )

    monkeypatch.setattr(
        migrations,
        "migrate_bitrix_crm_entities",
        lambda _client, _entities, _category_ids: calls.append("migrate_bitrix_crm_entities"),
    )

    def _unexpected_reconciliation(_client: object) -> int:
        pytest.fail("recurring lifecycle reconciliation ran during graph initialization")

    monkeypatch.setattr(
        migrations,
        "reconcile_source_record_lifecycle",
        _unexpected_reconciliation,
    )
    monkeypatch.setattr(
        migrations,
        "reconcile_projection_relationship_lifecycle",
        _unexpected_reconciliation,
    )

    migrations.apply_data_migrations(
        client,
        bitrix_source_instance_id="bitrix-primary",
        bitrix_crm_category_entities={"2": "speedzone"},
        included_bitrix_crm_category_ids=["2"],
    )

    assert calls == [
        "backfill_record_type_subtypes",
        "backfill_missing_person_completeness_scores",
        "migrate_bitrix_chat_source",
        "migrate_crm_deal_stage_projection",
        "migrate_bitrix_crm_entities",
        "migrate_fundbox_source_keys",
        "migrate_source_record_lifecycle",
        "migrate_source_record_source_instances",
        "migrate_identifier_scopes",
        "migrate_projection_relationship_lifecycle",
        "migrate_identity_link_revision_baseline",
    ]


def test_legacy_registry_bootstrap_is_owned_by_migration_callback_after_dispatch_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    client = _Client()
    monkeypatch.setattr(main, "get_settings", lambda: cast(Settings, object()))
    monkeypatch.setattr(main, "get_ingestion_config", lambda: IngestionConfig())
    monkeypatch.setattr(main, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(main, "apply_schema", lambda _client: calls.append("schema"))
    monkeypatch.setattr(
        main, "bootstrap_entities_and_sources", lambda _client: calls.append("sources")
    )
    monkeypatch.setattr(
        main, "bootstrap_legacy_bitrix_source_instance", lambda _client: calls.append("registry")
    )

    def _migration(_client: object, *, ensure_legacy_registration: object) -> None:
        calls.append("dispatch_blocked")
        ensure_legacy_registration()
        calls.append("migration_complete")

    monkeypatch.setattr(main, "migrate_ingestion_control_instances", _migration)
    monkeypatch.setattr(
        main, "assert_ingestion_control_ready", lambda _client: calls.append("ready")
    )
    monkeypatch.setattr(main, "apply_data_migrations", lambda _client, **_kwargs: None)
    monkeypatch.setattr(main, "apply_deferred_source_record_constraints", lambda _client: 0)
    monkeypatch.setattr(main, "apply_deferred_identifier_scope_constraints", lambda _client: 0)

    main.initialize_ingestion_graph()

    assert calls[:5] == ["schema", "sources", "dispatch_blocked", "registry", "migration_complete"]
