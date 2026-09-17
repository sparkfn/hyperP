"""Explicit, non-scheduled restoration runner for dormant historical validation."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from ci_support.selection_manifest import HISTORICAL_PROFILE  # noqa: E402

EnvironmentName = Literal["api", "ingestion", "intelligence"]
Command = tuple[str, ...]
_REQUIRED_TARGET_ENV = (
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI",
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER",
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD",
    "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_SERVICE_HOST",
    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_URI",
    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_USER",
    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_PASSWORD",
    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_SERVICE_HOST",
)
_MYPY_OVERRIDE_MARKER = (
    "# Issue #440: exact dormant modules are not followed by default active mypy."
)


@dataclass(frozen=True)
class HistoricalCommand:
    """One isolated historical command and its explicit no-skip policy."""

    argv: Command
    environment: EnvironmentName
    require_no_skips: bool = False


def _uv_run(package: str, *arguments: str) -> Command:
    return ("uv", "run", "--package", package, *arguments)


def _commands(mypy_config: str) -> tuple[HistoricalCommand, ...]:
    historical_neo4j = (
        "services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_mutation_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rollback_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_integration_neo4j.py",
    )
    intelligence_neo4j = (
        "services/intelligence/tests/test_crm_activities_neo4j.py",
        "services/intelligence/tests/test_crm_activities_cleanup_neo4j.py",
    )
    ingestion = "profile-unifier-ingestion"
    return (
        HistoricalCommand(
            (
                "uv",
                "sync",
                "--frozen",
                "--group",
                "dev",
                "--group",
                "training",
                "--package",
                ingestion,
            ),
            "ingestion",
        ),
        HistoricalCommand(
            _uv_run(ingestion, "python", "-c", "import numpy; print(numpy.__version__)"),
            "ingestion",
        ),
        HistoricalCommand(
            _uv_run(ingestion, "ruff", "check", "services/ingestion/src"),
            "ingestion",
        ),
        HistoricalCommand(
            _uv_run(ingestion, "ruff", "format", "--check", "services/ingestion/src"),
            "ingestion",
        ),
        HistoricalCommand(
            _uv_run(
                ingestion,
                "mypy",
                "--config-file",
                mypy_config,
                "--strict",
                "services/ingestion/src",
            ),
            "ingestion",
        ),
        HistoricalCommand(
            _uv_run(
                ingestion,
                "pytest",
                "services/ingestion/tests/test_sales_prediction_logistic.py",
                "services/ingestion/tests/test_sales_prediction_evaluator.py",
                "-q",
                "-rs",
            ),
            "ingestion",
            require_no_skips=True,
        ),
        HistoricalCommand(
            _uv_run(
                ingestion,
                "python",
                "scripts/wait_for_neo4j.py",
                "--uri-env",
                "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI",
                "--user-env",
                "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_USER",
                "--password-env",
                "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_PASSWORD",
                "--timeout-seconds",
                "90",
            ),
            "ingestion",
        ),
        HistoricalCommand(
            _uv_run(ingestion, "pytest", *historical_neo4j, "-q", "-rs"),
            "ingestion",
            require_no_skips=True,
        ),
        HistoricalCommand(
            _uv_run(ingestion, "pytest", "services/ingestion/tests", "-q"),
            "ingestion",
        ),
        HistoricalCommand(
            ("uv", "sync", "--frozen", "--group", "dev", "--package", "profile-unifier-api"),
            "api",
        ),
        HistoricalCommand(
            _uv_run(
                "profile-unifier-api",
                "pytest",
                "services/api/tests/test_crm_deal_identity_repair_reader_classification.py",
                "-q",
            ),
            "api",
        ),
        HistoricalCommand(
            ("uv", "sync", "--frozen", "--group", "dev", "--package", "hyperp-intelligence"),
            "intelligence",
        ),
        HistoricalCommand(
            _uv_run(
                "hyperp-intelligence",
                "python",
                "scripts/wait_for_neo4j.py",
                "--uri-env",
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI",
                "--user-env",
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER",
                "--password-env",
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD",
                "--timeout-seconds",
                "90",
            ),
            "intelligence",
        ),
        HistoricalCommand(
            _uv_run(
                "hyperp-intelligence",
                "ruff",
                "format",
                "--check",
                "services/intelligence/src",
                "services/intelligence/tests",
            ),
            "intelligence",
        ),
        HistoricalCommand(
            _uv_run(
                "hyperp-intelligence",
                "ruff",
                "check",
                "services/intelligence/src",
                "services/intelligence/tests",
            ),
            "intelligence",
        ),
        HistoricalCommand(
            _uv_run("hyperp-intelligence", "mypy", "--strict", "services/intelligence/src"),
            "intelligence",
        ),
        HistoricalCommand(
            _uv_run("hyperp-intelligence", "pytest", *intelligence_neo4j, "-q", "-rs"),
            "intelligence",
            require_no_skips=True,
        ),
        HistoricalCommand(
            _uv_run("hyperp-intelligence", "pytest", "services/intelligence/tests", "-q"),
            "intelligence",
        ),
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


def _command_environment(name: EnvironmentName) -> dict[str, str]:
    environment = {
        **os.environ,
        "HYPERP_CI_PROFILE": HISTORICAL_PROFILE,
        "HYPERP_HISTORICAL_REACTIVATION_ACK": "1",
        "UV_PROJECT_ENVIRONMENT": f".venv-historical-{name}",
    }
    if name == "intelligence":
        environment.update(
            {
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI": environment[
                    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_URI"
                ],
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_USER": environment[
                    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_USER"
                ],
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_PASSWORD": environment[
                    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_PASSWORD"
                ],
                "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST": environment[
                    "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_SERVICE_HOST"
                ],
            }
        )
    return environment


def _run(command: HistoricalCommand) -> None:
    result = subprocess.run(
        command.argv,
        cwd=_REPOSITORY_ROOT,
        env=_command_environment(command.environment),
        check=False,
        capture_output=command.require_no_skips,
        text=True,
    )
    if command.require_no_skips:
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if "skipped" in (result.stdout + result.stderr).lower():
            raise RuntimeError("required historical validation skipped one or more tests")
    result.check_returncode()


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
        print(
            "historical-command="
            f"environment={command.environment} argv={' '.join(command.argv)}"
        )
    if not args.execute:
        return 0
    try:
        for command in commands:
            _run(command)
    finally:
        if generated is not None:
            generated.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
