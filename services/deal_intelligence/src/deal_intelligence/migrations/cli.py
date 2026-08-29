"""Explicit Alembic CLI entry point; importing this module never runs migrations."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

from deal_intelligence.migrations.conventions import MIGRATION_LANES
from deal_intelligence.settings import get_settings

ALEMBIC_VERSION_TABLE = "deal_intelligence_alembic_version"


def alembic_config(database_url: URL | None = None) -> Config:
    """Build installed-package Alembic configuration without logging credentials.

    ``database_url`` lets tests supply their already-validated disposable target
    directly, instead of changing process environment state.
    """
    config = Config()
    config.set_main_option("script_location", str(migration_package_directory()))
    config.set_main_option(
        "version_locations", os.pathsep.join(str(path) for path in migration_version_locations())
    )
    config.set_main_option("path_separator", "os")
    url = database_url or make_url(get_settings().sqlalchemy_database_url())
    rendered_url = url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", rendered_url.replace("%", "%%"))
    config.set_main_option("version_table", ALEMBIC_VERSION_TABLE)
    return config


def migration_package_directory() -> Path:
    """Locate the installed ``deal_intelligence.migrations`` package directory."""
    return Path(__file__).resolve().parent


def migration_version_locations() -> tuple[Path, ...]:
    """Return every reserved migration lane from the installed package tree."""
    versions_directory = migration_package_directory() / "versions"
    return tuple(versions_directory / lane.directory_name for lane in MIGRATION_LANES)


def configured_version_table(config: Config) -> str:
    """Return the required package-owned Alembic version table name."""
    version_table = config.get_main_option("version_table")
    if version_table is None or not version_table.strip():
        raise RuntimeError("Alembic requires a nonempty package-owned version_table")
    return version_table


def main() -> None:
    """Run an explicit migration command; default to upgrading all current heads."""
    parser = argparse.ArgumentParser(prog="deal-intelligence-migrate")
    parser.add_argument(
        "command", choices=("upgrade", "current", "heads"), nargs="?", default="upgrade"
    )
    parser.add_argument("target", nargs="?", default="heads")
    arguments = parser.parse_args()
    config = alembic_config()
    if arguments.command == "upgrade":
        command.upgrade(config, arguments.target)
    elif arguments.command == "current":
        command.current(config)
    else:
        command.heads(config)
