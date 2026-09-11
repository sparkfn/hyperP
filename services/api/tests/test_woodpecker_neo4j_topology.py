"""Parsed Woodpecker bounded-validation topology contract for issue #418."""

from __future__ import annotations

from itertools import combinations
import subprocess
import sys
from pathlib import Path
from typing import cast

import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOW_NAMES = ("pr.yaml", "main.yaml")
_SHARDS = ("projection", "ledger-310", "census-migration-api", "repair-mapping")
_SHARD_STEP_NAMES = tuple(f"neo4j-{shard}-checks" for shard in _SHARDS)
_ROOT_STEP_NAMES = (
    "python-checks",
    "neo4j-projection-checks",
    "neo4j-ledger-310-checks",
    "neo4j-census-migration-api-checks",
    "neo4j-repair-mapping-checks",
)
_NEO4J_SERVICE_SETTINGS = {
    "NEO4J_PLUGINS": "[]",
    "NEO4J_server_http_enabled": "false",
    "NEO4J_server_memory_heap_initial__size": "128m",
    "NEO4J_server_memory_heap_max__size": "384m",
    "NEO4J_server_memory_pagecache_size": "128m",
}

_NEO4J_SHARDS = {
    "projection": ("neo4j-projection", ("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",), 0),
    "ledger-310": ("neo4j-ledger-310", ("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST",), 0),
    "census-migration-api": (
        "neo4j-census-migration-api",
        (
            "HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST HYPERP_NEO4J_CRM_METRICS_TEST "
            "HYPERP_NEO4J_PERSON_LIST_TEST HYPERP_NEO4J_PERSON_COMPLETENESS_TEST "
            "HYPERP_NEO4J_LOYALTY_POINTS_TEST HYPERP_NEO4J_CRM_DEAL_COUNT_TEST "
            "HYPERP_NEO4J_CONTROL_MIGRATION_TEST HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST "
            "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST"
        ).split(),
        3,
    ),
    "repair-mapping": (
        "neo4j-repair-mapping",
        ("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST", "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST"),
        0,
    ),
}
_API_NEO4J_TESTS = (
    "person_identifiers_neo4j person_crm_metrics_neo4j persons_list_neo4j_234 "
    "persons_list_possible_match_neo4j persons_list_plan_neo4j "
    "identity_link_revisions_neo4j person_graph_neo4j"
).split()
_INGESTION_NEO4J_TESTS = (
    "person_completeness_migration_neo4j loyalty_points_migration_neo4j "
    "crm_deal_count_migration_neo4j ingestion_control_instance_migration_neo4j "
    "identifier_scope_migrations_neo4j identifier_scope_schema_neo4j "
    "identity_link_revision_baseline_neo4j standalone_crm_census_neo4j "
    "standalone_crm_lane_a_schema_neo4j standalone_crm_source_child_integration_neo4j "
    "crm_company_membership_neo4j crm_tenant_mapping_repository_neo4j_preparation "
    "crm_tenant_mapping_repository_neo4j_lifecycle "
    "crm_tenant_mapping_repository_neo4j_freshness_integrity "
    "crm_tenant_mapping_repository_neo4j_strictness crm_tenant_projection_repository_neo4j "
    "crm_tenant_activation_neo4j crm_deal_identity_repair_mutation_neo4j "
    "crm_deal_identity_repair_verification_neo4j crm_deal_identity_repair_rollback_neo4j "
    "crm_deal_identity_repair_integration_neo4j"
).split()
_NEO4J_SUITE_MANIFEST = frozenset(
    {(f"services/api/tests/test_{name}.py", "") for name in _API_NEO4J_TESTS}
    | {(f"services/ingestion/tests/test_{name}.py", "") for name in _INGESTION_NEO4J_TESTS}
    | {
        ("services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py", "test_310_"),
        ("services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py", "not test_310_"),
    }
)
_PYTHON_COMMANDS = (
    "uv sync --frozen",
    "uv run --package profile-unifier-api ruff check services/api/src",
    "uv run --package profile-unifier-api ruff format --check services/api/src",
    "uv run --package profile-unifier-ingestion ruff check services/ingestion/src",
    "uv run --package profile-unifier-ingestion ruff format --check services/ingestion/src",
    "uv run --package profile-unifier-api mypy --strict services/api/src",
    "uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src",
    "uv run --package profile-unifier-api pytest "
    "services/api/tests/test_persons_list_queries.py::"
    "test_person_list_preferred_address_hydration_does_not_expand_provenance_edges",
    "uv run --package profile-unifier-api pytest services/api/tests",
    "uv sync --frozen --group training",
    "uv run --package profile-unifier-ingestion pytest services/ingestion/tests",
)
_INTELLIGENCE_COMMANDS = (
    "uv sync --frozen --group dev",
    "uv run --package hyperp-intelligence ruff format --check "
    "services/intelligence/src services/intelligence/tests",
    "uv run --package hyperp-intelligence ruff check "
    "services/intelligence/src services/intelligence/tests",
    "uv run --package hyperp-intelligence mypy --strict services/intelligence/src",
    "uv run --package hyperp-intelligence pytest services/intelligence/tests -q",
)
_IDENTIFIER_SCOPE_SCHEMA_COMMAND = (
    "uv run --package profile-unifier-ingestion pytest "
    "services/ingestion/tests/test_identifier_scope_schema_neo4j.py -q"
)
_PROJECTION_INTELLIGENCE_COMMAND = (
    "uv run --package hyperp-intelligence pytest services/intelligence/tests "
    "-k crm_activities_neo4j -q"
)


def _workflow_document(workflow_name: str) -> dict[str, object]:
    raw_document = yaml.safe_load((_REPOSITORY_ROOT / ".woodpecker" / workflow_name).read_text())
    assert isinstance(raw_document, dict)
    return cast(dict[str, object], raw_document)


def _workflow_steps(workflow: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_steps = workflow.get("steps")
    assert isinstance(raw_steps, list)
    steps: dict[str, dict[str, object]] = {}
    for raw_step in cast(list[object], raw_steps):
        assert isinstance(raw_step, dict)
        step = cast(dict[str, object], raw_step)
        name = step.get("name")
        assert isinstance(name, str) and name not in steps
        steps[name] = step
    return steps


def _dependency_details(dependency: object) -> tuple[str, bool]:
    if isinstance(dependency, str):
        return dependency, False
    assert isinstance(dependency, dict)
    details = cast(dict[str, object], dependency)
    name = details.get("name")
    optional = details.get("optional")
    assert isinstance(name, str) and isinstance(optional, bool)
    assert set(details) == {"name", "optional"}
    return name, optional


def _dependencies(step: dict[str, object]) -> list[tuple[str, bool]]:
    raw_dependencies = step.get("depends_on")
    assert isinstance(raw_dependencies, list)
    return [_dependency_details(dependency) for dependency in raw_dependencies]


def _commands(step: dict[str, object]) -> list[str]:
    raw_commands = step.get("commands")
    assert isinstance(raw_commands, list) and all(
        isinstance(command, str) for command in raw_commands
    )
    return cast(list[str], raw_commands)


def _assert_acyclic_dependencies(steps: dict[str, dict[str, object]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(step_name: str) -> None:
        assert step_name not in visiting
        if step_name in visited:
            return
        visiting.add(step_name)
        for dependency_name, _optional in _dependencies(steps[step_name]):
            assert dependency_name in steps and dependency_name != step_name
            visit(dependency_name)
        visiting.remove(step_name)
        visited.add(step_name)

    for step_name in steps:
        visit(step_name)


def _waves(
    steps: dict[str, dict[str, object]], active_steps: frozenset[str]
) -> list[frozenset[str]]:
    completed: set[str] = set()
    waiting: set[str] = set(active_steps)
    waves: list[frozenset[str]] = []
    while waiting:
        ready = frozenset(
            step_name
            for step_name in waiting
            if {
                dependency_name
                for dependency_name, optional in _dependencies(steps[step_name])
                if dependency_name in active_steps or not optional
            }
            <= completed
        )
        assert ready
        waves.append(ready)
        completed.update(ready)
        waiting.difference_update(ready)
    return waves


def _readiness_command(family: str) -> str:
    assert (_REPOSITORY_ROOT / "scripts" / "wait_for_neo4j.py").is_file()
    return (
        "uv run --package profile-unifier-ingestion python scripts/wait_for_neo4j.py "
        f"--uri-env {family}_URI --user-env {family}_USER "
        f"--password-env {family}_PASSWORD --timeout-seconds 90"
    )


def _assert_readiness_precedes_pytest(step: dict[str, object], family: str) -> None:
    commands = _commands(step)
    assert commands[:2] == ["uv sync --frozen", _readiness_command(family)]
    assert commands.count(_readiness_command(family)) == 1
    pytest_indexes = [index for index, command in enumerate(commands) if " pytest " in command]
    assert pytest_indexes
    assert 1 < min(pytest_indexes)


def _neo4j_manifest(steps: dict[str, dict[str, object]]) -> frozenset[tuple[str, str]]:
    manifest: set[tuple[str, str]] = set()
    for shard in _NEO4J_SHARDS:
        commands = steps[f"neo4j-{shard}-checks"].get("commands")
        assert isinstance(commands, list)
        for command in commands:
            assert isinstance(command, str)
            if " pytest " not in command:
                continue
            selector = ""
            if " -k '" in command:
                selector = command.split(" -k '", 1)[1].split("'", 1)[0]
            for token in command.split():
                if token.startswith("services/") and "_neo4j" in token and token.endswith(".py"):
                    manifest.add((token, selector))
    return frozenset(manifest)


def test_woodpecker_neo4j_readiness_timeout_rejects_non_finite_values() -> None:
    script = _REPOSITORY_ROOT / "scripts" / "wait_for_neo4j.py"
    for timeout in ("nan", "inf", "-inf"):
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--uri-env",
                "HYPERP_UNUSED_NEO4J_URI",
                "--user-env",
                "HYPERP_UNUSED_NEO4J_USER",
                "--password-env",
                "HYPERP_UNUSED_NEO4J_PASSWORD",
                "--timeout-seconds",
                timeout,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        assert result.returncode != 0
        assert "finite positive number" in result.stderr


def test_woodpecker_bounded_validation_dag_has_exact_two_wave_schedule() -> None:
    workflows = {name: _workflow_document(name) for name in _WORKFLOW_NAMES}
    pr_second_wave = frozenset({"intelligence-checks", "frontend-checks"})
    for workflow_name, workflow in workflows.items():
        is_pr = workflow_name == "pr.yaml"
        frontend_name = "frontend-checks" if is_pr else "frontend-build"
        steps = _workflow_steps(workflow)
        assert set(steps) == {*_ROOT_STEP_NAMES, "intelligence-checks", frontend_name}
        _assert_acyclic_dependencies(steps)
        assert all(steps[step_name].get("depends_on") == [] for step_name in _ROOT_STEP_NAMES)
        assert workflow.get("when") == (
            {"event": ["pull_request"]}
            if is_pr
            else {"event": ["push"], "branch": ["main"]}
        )
        expected_dependencies = [
            "python-checks",
            *({"name": name, "optional": True} for name in _SHARD_STEP_NAMES),
        ]
        if is_pr:
            assert steps["intelligence-checks"].get("depends_on") == expected_dependencies
            assert steps[frontend_name].get("depends_on") == expected_dependencies
        else:
            assert steps["intelligence-checks"].get("depends_on") == list(_ROOT_STEP_NAMES)
            assert steps[frontend_name].get("depends_on") == list(_ROOT_STEP_NAMES)
        for step_name, step in steps.items():
            if is_pr and step_name in _SHARD_STEP_NAMES:
                assert step.get("when") == [
                    {"path": {"exclude": ["docs/**", "services/frontend2/**"]}}
                ]
            else:
                assert "when" not in step
        expected_second_wave = frozenset({"intelligence-checks", frontend_name})
        assert _waves(steps, frozenset(steps)) == [
            frozenset(_ROOT_STEP_NAMES),
            expected_second_wave,
        ]
        assert max(map(len, _waves(steps, frozenset(steps)))) == 5
    pr_steps = _workflow_steps(workflows["pr.yaml"])
    for size in range(len(_SHARD_STEP_NAMES) + 1):
        for surviving_shards in combinations(_SHARD_STEP_NAMES, size):
            active_steps = frozenset({"python-checks", *surviving_shards, *pr_second_wave})
            assert _waves(pr_steps, active_steps) == [
                frozenset({"python-checks", *surviving_shards}),
                pr_second_wave,
            ]
            assert max(map(len, _waves(pr_steps, active_steps))) == max(1 + size, 2)


def test_woodpecker_validation_commands_preserve_pr_main_differences() -> None:
    pr_steps = _workflow_steps(_workflow_document("pr.yaml"))
    main_steps = _workflow_steps(_workflow_document("main.yaml"))
    assert _commands(pr_steps["python-checks"]) == list(_PYTHON_COMMANDS)
    assert _commands(main_steps["python-checks"]) == [
        *_PYTHON_COMMANDS,
        "uv sync --frozen --no-dev --package profile-unifier-api",
        "uv sync --frozen --no-dev --package profile-unifier-ingestion",
    ]
    assert _commands(pr_steps["intelligence-checks"]) == list(_INTELLIGENCE_COMMANDS)
    assert _commands(main_steps["intelligence-checks"]) == [
        *_INTELLIGENCE_COMMANDS,
        "uv sync --frozen --no-dev --package hyperp-intelligence",
    ]
    assert _commands(pr_steps["frontend-checks"]) == (
        "cd services/frontend2|npm install --legacy-peer-deps|npm run typecheck|"
        "npx eslint src|npm test"
    ).split("|")
    assert _commands(main_steps["frontend-build"]) == (
        "cd services/frontend2|npm install --legacy-peer-deps|npm test|npm run build"
    ).split("|")
    assert all(
        _commands(pr_steps[name]) == _commands(main_steps[name]) for name in _SHARD_STEP_NAMES
    )
    assert _PROJECTION_INTELLIGENCE_COMMAND in _commands(pr_steps["neo4j-projection-checks"])


def test_woodpecker_neo4j_shards_are_complete_isolated_and_parity_checked() -> None:
    workflows = {name: _workflow_document(name) for name in _WORKFLOW_NAMES}
    manifests: dict[str, frozenset[tuple[str, str]]] = {}
    for workflow_name, workflow in workflows.items():
        services = workflow.get("services")
        assert isinstance(services, list)
        assert len(services) == len(_NEO4J_SHARDS)
        service_by_name: dict[str, dict[str, object]] = {}
        for raw_service in cast(list[object], services):
            assert isinstance(raw_service, dict)
            service = cast(dict[str, object], raw_service)
            name = service.get("name")
            assert isinstance(name, str)
            service_by_name[name] = service
        steps = _workflow_steps(workflow)
        assert len(service_by_name) == len(_NEO4J_SHARDS)
        python_step_names = (*_ROOT_STEP_NAMES, "intelligence-checks")
        python_environments: list[dict[str, object]] = []
        for step_name in python_step_names:
            step = steps[step_name]
            assert step.get("image") == "ghcr.io/astral-sh/uv:python3.12-bookworm"
            assert "workspace" not in step
            environment = step.get("environment")
            assert isinstance(environment, dict)
            typed_environment = cast(dict[str, object], environment)
            assert typed_environment.get("UV_PROJECT_ENVIRONMENT") == f".venv-{step_name}"
            assert typed_environment.get("PYTHONDONTWRITEBYTECODE") == "1"
            python_environments.append(typed_environment)
        assert len({item["UV_PROJECT_ENVIRONMENT"] for item in python_environments}) == len(
            python_environments
        )
        python_environment = python_environments[0]
        assert python_environment.get("PYTEST_ADDOPTS") == (
            "-o cache_dir=.pytest_cache-python-checks"
        )
        passwords: set[str] = set()
        for shard, contract in _NEO4J_SHARDS.items():
            service_name, families, readiness_index = contract
            service = service_by_name[service_name]
            environment = service.get("environment")
            assert service.get("image") == "neo4j:5.26-community"
            assert isinstance(environment, dict)
            service_environment = cast(dict[str, object], environment)
            assert {
                key: value for key, value in service_environment.items() if key != "NEO4J_AUTH"
            } == _NEO4J_SERVICE_SETTINGS
            auth = service_environment.get("NEO4J_AUTH")
            assert isinstance(auth, str) and auth.startswith("neo4j/")
            password = auth.removeprefix("neo4j/")
            assert password and password not in passwords
            passwords.add(password)
            step = steps[f"neo4j-{shard}-checks"]
            step_environment = step.get("environment")
            assert step.get("depends_on") == []
            readiness_family = families[readiness_index]
            _assert_readiness_precedes_pytest(step, readiness_family)
            if shard == "census-migration-api":
                commands = _commands(step)
                assert commands[2] == _IDENTIFIER_SCOPE_SCHEMA_COMMAND
                assert commands.count(_IDENTIFIER_SCOPE_SCHEMA_COMMAND) == 1
            assert isinstance(step_environment, dict)
            shard_environment = cast(dict[str, object], step_environment)
            assert shard_environment.get("UV_PROJECT_ENVIRONMENT") == f".venv-neo4j-{shard}-checks"
            assert shard_environment.get("PYTEST_ADDOPTS") == (
                f"-o cache_dir=.pytest_cache-neo4j-{shard}-checks"
            )
            assert shard_environment.get("PYTHONDONTWRITEBYTECODE") == "1"
            assert shard_environment.get("HYPERP_NEO4J_PERSON_LIST_TEST_ALLOW_SCHEMA_MUTATION") == (
                "1" if shard == "census-migration-api" else None
            )
            expected_neo4j_keys = {
                f"{family}_{suffix}"
                for family in families
                for suffix in ("URI", "USER", "PASSWORD", "SERVICE_HOST")
            }
            if shard == "census-migration-api":
                expected_neo4j_keys.add("HYPERP_NEO4J_PERSON_LIST_TEST_ALLOW_SCHEMA_MUTATION")
            actual_neo4j_keys = {
                key for key in shard_environment if key.startswith("HYPERP_NEO4J_")
            }
            assert actual_neo4j_keys == expected_neo4j_keys
            for family in families:
                assert shard_environment.get(f"{family}_URI") == f"bolt://{service_name}:7687"
                assert shard_environment.get(f"{family}_USER") == "neo4j"
                assert shard_environment.get(f"{family}_PASSWORD") == password
                assert shard_environment.get(f"{family}_SERVICE_HOST") == service_name
            assert {
                key.removesuffix("_SERVICE_HOST")
                for key in shard_environment
                if key.endswith("_SERVICE_HOST") and key.startswith("HYPERP_NEO4J_")
            } == set(families)

        manifests[workflow_name] = _neo4j_manifest(steps)
        rendered = str(workflow).lower()
        prohibited_terms = (
            "privileged",
            "volumes",
            "docker compose",
            "pytest-xdist",
            "xdist",
            "docker.sock",
            "retry",
            "allow_failure",
            "ignore_failure",
            "continue-on-error",
            "|| true",
        )
        for prohibited in prohibited_terms:
            assert prohibited not in rendered
        assert all(not ({"failure", "detach", "retry"} & set(step)) for step in steps.values())

    assert manifests["pr.yaml"] == _NEO4J_SUITE_MANIFEST
    assert manifests["main.yaml"] == _NEO4J_SUITE_MANIFEST
    assert manifests["pr.yaml"] == manifests["main.yaml"]
    assert len({path for path, _selector in manifests["pr.yaml"]}) == 29
