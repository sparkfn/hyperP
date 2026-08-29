"""Tests for shared-platform schema inventory and migration graph."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from deal_intelligence.migrations.cli import (
    ALEMBIC_VERSION_TABLE,
    alembic_config,
    configured_version_table,
    migration_package_directory,
    migration_version_locations,
)
from deal_intelligence.migrations.conventions import MIGRATION_LANES, lane_for_branch_label
from deal_intelligence.platform.schema import (
    EXCLUDED_DOMAIN_TABLE_NAMES,
    metadata,
    schema_inventory,
)
from deal_intelligence.platform.types import SourceInstanceRegistration
from sqlalchemy.engine import make_url


def test_schema_inventory_contains_only_shared_platform_tables() -> None:
    inventory = schema_inventory()
    assert inventory == {
        "source_instances",
        "process_runs",
        "process_units",
        "checkpoints",
        "leases",
        "terminal_accounting",
        "schema_readiness",
        "process_heartbeats",
        "migration_bookkeeping",
    }
    assert inventory.isdisjoint(EXCLUDED_DOMAIN_TABLE_NAMES)
    assert all(table.schema == "deal_intelligence" for table in metadata.sorted_tables)


def test_shared_platform_schema_has_constraints_and_indexes() -> None:
    tables = {table.name: table for table in metadata.sorted_tables}
    source_constraint_names = {
        constraint.name for constraint in tables["source_instances"].constraints
    }
    assert {
        "source_instances_source_system_slug",
        "source_instances_instance_key_slug",
        "source_instances_display_name_safe",
    }.issubset(source_constraint_names)
    assert any(
        index.name == "process_runs_component_status_index"
        for index in tables["process_runs"].indexes
    )
    assert any(
        index.name == "process_units_run_status_index" for index in tables["process_units"].indexes
    )
    assert any(index.name == "leases_owner_run_index" for index in tables["leases"].indexes)
    assert any(
        constraint.name == "process_units_terminal_finished_at"
        for constraint in tables["process_units"].constraints
    )
    assert any(
        constraint.name == "terminal_accounting_counts_balance"
        for constraint in tables["terminal_accounting"].constraints
    )


def test_migration_graph_has_baseline_and_platform_head() -> None:
    directory = ScriptDirectory.from_config(alembic_config())
    assert directory.get_heads() == ["di_0002_shared_platform"]
    assert directory.get_revision("di_0001_baseline") is not None
    assert directory.get_revision("di_0002_shared_platform") is not None


def test_alembic_config_is_package_local_and_accepts_an_explicit_url() -> None:
    config = alembic_config(make_url("postgresql+psycopg://test_user@test-host/di_test"))
    script_location = config.get_main_option("script_location")
    version_locations = config.get_main_option("version_locations")
    package_directory = migration_package_directory()
    assert script_location is not None
    assert Path(script_location) == package_directory
    assert package_directory.is_absolute()
    assert package_directory.is_dir()
    assert version_locations is not None
    assert tuple(Path(location) for location in version_locations.split(os.pathsep)) == (
        migration_version_locations()
    )
    assert all(path.is_absolute() and path.is_dir() for path in migration_version_locations())
    assert all(path.is_relative_to(package_directory) for path in migration_version_locations())
    assert config.config_file_name is None
    assert config.get_main_option("version_table") == ALEMBIC_VERSION_TABLE
    assert configured_version_table(config) == ALEMBIC_VERSION_TABLE


def test_configured_version_table_rejects_missing_or_blank_values() -> None:
    missing = Config()
    blank = Config()
    blank.set_main_option("version_table", " ")
    with pytest.raises(RuntimeError, match="nonempty"):
        configured_version_table(missing)
    with pytest.raises(RuntimeError, match="nonempty"):
        configured_version_table(blank)


@pytest.mark.parametrize(
    ("source_system", "instance_key", "display_name"),
    (
        ("bitrix_crm", "test-instance_1", "Test Instance"),
        ("pos", "production", "Local POS"),
    ),
)
def test_source_instance_registration_accepts_bounded_lowercase_identifiers(
    source_system: str, instance_key: str, display_name: str
) -> None:
    registration = SourceInstanceRegistration(source_system, instance_key, display_name)
    assert registration.is_enabled is False


@pytest.mark.parametrize(
    ("source_system", "instance_key", "display_name"),
    (
        ("Bitrix", "test", "Test"),
        ("bitrix", "test instance", "Test"),
        ("bitrix", "user:secret@host", "Test"),
        ("bitrix", "test", " https://example.test"),
        ("bitrix", "test", "user:secret@host"),
        ("x" * 81, "test", "Test"),
        ("bitrix", "x" * 256, "Test"),
        ("bitrix", "test", "x" * 256),
    ),
)
def test_source_instance_registration_rejects_secret_or_invalid_values(
    source_system: str, instance_key: str, display_name: str
) -> None:
    with pytest.raises(ValueError):
        SourceInstanceRegistration(source_system, instance_key, display_name)


def test_reserved_migration_lanes_independently_branch_from_platform() -> None:
    labels = tuple(lane.branch_label for lane in MIGRATION_LANES)
    assert labels == (
        "baseline",
        "platform",
        "identity",
        "deal_stage",
        "activity",
        "historical_import",
        "artifact",
        "projection_outbox",
        "ownership",
    )
    for label in labels[2:]:
        assert lane_for_branch_label(label).depends_on == ("platform",)
