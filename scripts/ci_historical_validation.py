"""Explicit, non-scheduled restoration runner for dormant historical validation."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from ci_support.selection_manifest import HISTORICAL_PROFILE  # noqa: E402

Command = tuple[str, ...]
_REQUIRED_TARGET_ENV = (
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI",
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER",
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD",
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST",
)
_MYPY_OVERRIDE_MARKER = (
    "# Issue #440: exact dormant modules are not followed by default active mypy."
)


def _uv_run(package: str, *arguments: str) -> Command:
    return ("uv", "run", "--package", package, *arguments)


def _commands(mypy_config: str) -> tuple[Command, ...]:
    historical_neo4j = (
        "services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_mutation_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rollback_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_integration_neo4j.py",
    )
    return (
        ("uv", "sync", "--frozen", "--group", "training"),
        _uv_run(
            "profile-unifier-ingestion",
            "ruff",
            "check",
            "services/ingestion/src",
        ),
        _uv_run(
            "profile-unifier-ingestion",
            "ruff",
            "format",
            "--check",
            "services/ingestion/src",
        ),
        _uv_run(
            "profile-unifier-ingestion",
            "mypy",
            "--config-file",
            mypy_config,
            "--strict",
            "services/ingestion/src",
        ),
        _uv_run(
            "profile-unifier-api",
            "pytest",
            "services/api/tests/test_crm_deal_identity_repair_reader_classification.py",
            "-q",
        ),
        ("uv", "sync", "--frozen", "--package", "hyperp-intelligence", "--group", "dev"),
        _uv_run(
            "hyperp-intelligence",
            "ruff",
            "format",
            "--check",
            "services/intelligence/src",
            "services/intelligence/tests",
        ),
        _uv_run(
            "hyperp-intelligence",
            "ruff",
            "check",
            "services/intelligence/src",
            "services/intelligence/tests",
        ),
        _uv_run("hyperp-intelligence", "mypy", "--strict", "services/intelligence/src"),
        _uv_run("hyperp-intelligence", "pytest", "services/intelligence/tests", "-q"),
        _uv_run("profile-unifier-ingestion", "pytest", "services/ingestion/tests", "-q"),
        _uv_run("profile-unifier-ingestion", "pytest", *historical_neo4j, "-q"),
    )


def _historical_mypy_config() -> Path:
    source = (_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if _MYPY_OVERRIDE_MARKER not in source:
        raise RuntimeError("active-only mypy override is missing from pyproject.toml")
    historical = source.split(_MYPY_OVERRIDE_MARKER, maxsplit=1)[0].rstrip() + "\n"
    directory = _REPOSITORY_ROOT / ".ci-manifests"
    directory.mkdir(exist_ok=True)
    path = directory / "historical-mypy.toml"
    path.write_text(historical, encoding="utf-8")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acknowledge-dormant-reactivation", action="store_true")
    parser.add_argument("--disposable-neo4j", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser


def _validate(args: argparse.Namespace) -> None:
    if not args.acknowledge_dormant_reactivation or not args.disposable_neo4j:
        raise SystemExit(
            "refusing historical validation: pass both --acknowledge-dormant-reactivation "
            "and --disposable-neo4j before any setup"
        )
    if not args.execute:
        return
    missing = tuple(name for name in _REQUIRED_TARGET_ENV if not os.getenv(name))
    if missing:
        raise SystemExit(f"historical validation requires disposable Neo4j target env: {missing}")
    if os.getenv("HYPERP_HISTORICAL_DISPOSABLE_NEO4J") != "1":
        raise SystemExit("set HYPERP_HISTORICAL_DISPOSABLE_NEO4J=1 for an isolated target")


def main() -> int:
    args = _parser().parse_args()
    _validate(args)
    config = "<generated-historical-mypy-config>"
    generated: Path | None = None
    if args.execute:
        generated = _historical_mypy_config()
        config = str(generated)
    commands = _commands(config)
    for command in commands:
        print("historical-command=" + " ".join(command))
    if not args.execute:
        return 0
    env = {
        **os.environ,
        "HYPERP_CI_PROFILE": HISTORICAL_PROFILE,
        "HYPERP_HISTORICAL_REACTIVATION_ACK": "1",
    }
    try:
        for command in commands:
            subprocess.run(command, cwd=_REPOSITORY_ROOT, env=env, check=True)
    finally:
        if generated is not None:
            generated.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
