"""Parsed active-only Woodpecker topology and selection contracts for issue #440."""

from __future__ import annotations

import runpy
import tomllib
from pathlib import Path
from typing import cast

import yaml

from ci_support.selection_manifest import (
    ACTIVE_NODE_SENTINELS,
    ACTIVE_QUERY_SENTINELS,
    ACTIVE_SHARED_SOURCE_PATHS,
    HISTORICAL_SOURCE_PATHS,
    HISTORICAL_TEST_MODULES,
    validate_default_query_manifest,
)

_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = ("pr.yaml", "main.yaml")
_SHARDS = (
    "neo4j-projection-checks",
    "neo4j-census-migration-api-checks",
    "neo4j-tenant-mapping-checks",
)
_NEO4J_SETTINGS = {
    "NEO4J_PLUGINS": "[]",
    "NEO4J_server_http_enabled": "false",
    "NEO4J_server_memory_heap_initial__size": "128m",
    "NEO4J_server_memory_heap_max__size": "384m",
    "NEO4J_server_memory_pagecache_size": "128m",
}
_PR_PATH_FILTER = [{"path": {"exclude": ["docs/**", "services/frontend2/**"]}}]
_EXPECTED_PR_PYTHON_COMMANDS = (
    "uv sync --frozen --group dev --package profile-unifier-api "
    "--package profile-unifier-ingestion",
    "uv run --package profile-unifier-api ruff check services/api/src",
    "uv run --package profile-unifier-api ruff format --check services/api/src",
    "uv run --package profile-unifier-ingestion ruff check services/ingestion/src "
    "$(python scripts/ci_selection_manifest.py tool-excludes --tool ruff)",
    "uv run --package profile-unifier-ingestion ruff format --check services/ingestion/src "
    "$(python scripts/ci_selection_manifest.py tool-excludes --tool ruff)",
    "uv run --package profile-unifier-api mypy --strict services/api/src",
    "uv run --package profile-unifier-ingestion mypy --strict services/ingestion/src "
    "$(python scripts/ci_selection_manifest.py tool-excludes --tool mypy)",
    "mkdir -p .ci-manifests",
    "uv run --package profile-unifier-api pytest services/api/tests --collect-only -q "
    "> .ci-manifests/api-default-nodes.txt",
    "python scripts/ci_selection_manifest.py verify-nodes --service api "
    "--path .ci-manifests/api-default-nodes.txt",
    "uv run --package profile-unifier-ingestion pytest services/ingestion/tests --collect-only -q "
    "> .ci-manifests/ingestion-default-nodes.txt",
    "python scripts/ci_selection_manifest.py verify-nodes --service ingestion "
    "--path .ci-manifests/ingestion-default-nodes.txt",
    "uv run --package profile-unifier-api pytest services/api/tests",
    "uv run --package profile-unifier-ingestion pytest services/ingestion/tests "
    "--durations=25 --durations-min=1.0",
)
_MAIN_PRODUCTION_INSTALLS = (
    "uv sync --frozen --no-dev --package profile-unifier-api",
    "uv sync --frozen --no-dev --package profile-unifier-ingestion",
)
_EXPECTED_HISTORICAL_TRAINING_SYNC = (
    "uv",
    "sync",
    "--frozen",
    "--group",
    "dev",
    "--group",
    "training",
    "--package",
    "profile-unifier-ingestion",
)
_HISTORICAL_DORMANT_SOURCE_PATHS = frozenset(
    {
        "services/ingestion/src/graph/crm_deal_identity_repair_mutation_errors.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_verification_errors.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_mutation.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_rebase.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_rollback.py",
    }
)
_SHARED_ACTIVE_SOURCE_PATHS = frozenset(
    {
        "services/ingestion/src/graph/crm_deal_identity_repair_control.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_ledger.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_ledger_migration.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_ledger_records.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_control.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_integration.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_boundary_evidence.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_status_evidence.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_status_snapshot.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_verification.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_ledger.py",
    }
)
_EXPECTED_ACTIVE_NODE_SENTINELS = {
    "api": (
        "services/api/tests/test_active_api_reader_contract.py::"
        "test_api_authoritative_reader_parity_excludes_retired_links",
        "services/api/tests/test_mcp_app.py::"
        "test_mcp_tools_match_every_canonical_api_operation",
        "services/api/tests/test_ci_selection_gate.py::"
        "test_active_profile_rejects_explicit_historical_path_and_node_before_import",
        "services/api/tests/test_person_crm_metrics_neo4j.py::"
        "test_deal_metrics_query_uses_projected_stage_and_excludes_live_activity_records",
    ),
    "ingestion": (
        "services/ingestion/tests/test_active_publication_fencing_neo4j.py::"
        "test_publication_reservation_and_repair_claim_are_mutually_exclusive",
        "services/ingestion/tests/test_active_publication_fencing_neo4j.py::"
        "test_stale_publication_confirmation_fails_closed",
        "services/ingestion/tests/test_active_relationship_reader_contract.py::"
        "test_active_materializers_are_classified_and_filter_current_relationships",
        "services/ingestion/tests/test_bitrix_backfill_tasks.py::"
        "test_live_canvas_allows_deal_only_when_activities_are_reviewed_excluded",
        "services/ingestion/tests/test_scheduled_ingestion_tasks.py::"
        "test_successor_filters_executable_historical_activity_before_probing_or_publication",
        "services/ingestion/tests/test_resumable.py::"
        "test_checkpoint_can_advance_for_durable_dispositions",
        "services/ingestion/tests/test_sales_prediction_evaluator_no_numpy.py::"
        "test_evaluator_helpers_do_not_import_or_require_numpy",
        "services/ingestion/tests/test_standalone_crm_census_topology.py::"
        "test_default_off_configuration_and_authority_admission_fail_closed",
    ),
}


def _workflow(name: str) -> dict[str, object]:
    document = yaml.safe_load((_ROOT / ".woodpecker" / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast(dict[str, object], document)


def _steps(document: dict[str, object]) -> dict[str, dict[str, object]]:
    raw_steps = document.get("steps")
    assert isinstance(raw_steps, list)
    steps: dict[str, dict[str, object]] = {}
    for raw_step in raw_steps:
        assert isinstance(raw_step, dict)
        step = cast(dict[str, object], raw_step)
        name = step.get("name")
        assert isinstance(name, str) and name not in steps
        steps[name] = step
    return steps


def _commands(step: dict[str, object]) -> tuple[str, ...]:
    commands = step.get("commands")
    assert isinstance(commands, list) and all(isinstance(command, str) for command in commands)
    return tuple(cast(list[str], commands))


def _environment(step: dict[str, object]) -> dict[str, object]:
    environment = step.get("environment")
    assert isinstance(environment, dict)
    return cast(dict[str, object], environment)


def _dependency_details(item: object) -> tuple[str, bool]:
    if isinstance(item, str):
        return item, False
    assert isinstance(item, dict)
    name = item.get("name")
    optional = item.get("optional")
    assert isinstance(name, str) and isinstance(optional, bool)
    assert set(item) == {"name", "optional"}
    return name, optional


def _dependencies(step: dict[str, object]) -> tuple[tuple[str, bool], ...]:
    raw = step.get("depends_on")
    assert isinstance(raw, list)
    return tuple(_dependency_details(item) for item in raw)


def _python_step_names() -> tuple[str, ...]:
    return ("python-checks", *_SHARDS)


def test_workflows_keep_active_pr_main_boundaries_and_dependency_semantics() -> None:
    for name in _WORKFLOWS:
        document = _workflow(name)
        steps = _steps(document)
        frontend = "frontend-checks" if name == "pr.yaml" else "frontend-build"
        assert set(steps) == {"python-checks", *_SHARDS, frontend}
        expected_when = {"event": ["pull_request"]}
        if name == "main.yaml":
            expected_when = {"event": ["push"], "branch": ["main"]}
        assert document["when"] == expected_when
        assert all(steps[step]["depends_on"] == [] for step in _python_step_names())
        dependencies = _dependencies(steps[frontend])
        if name == "pr.yaml":
            assert dependencies == (
                ("python-checks", False),
                *((shard, True) for shard in _SHARDS),
            )
            assert all(steps[shard].get("when") == _PR_PATH_FILTER for shard in _SHARDS)
        else:
            assert dependencies == (
                ("python-checks", False),
                *((shard, False) for shard in _SHARDS),
            )
            assert all("when" not in steps[shard] for shard in _SHARDS)


def test_python_steps_have_unique_active_environment_and_cache_isolation() -> None:
    for name in _WORKFLOWS:
        steps = _steps(_workflow(name))
        environments: list[dict[str, object]] = []
        for step_name in _python_step_names():
            step = steps[step_name]
            environment = _environment(step)
            assert step["image"] == "ghcr.io/astral-sh/uv:python3.12-bookworm"
            assert environment["HYPERP_CI_PROFILE"] == "active"
            assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
            assert environment["UV_PROJECT_ENVIRONMENT"] == f".venv-{step_name}"
            assert environment["PYTEST_ADDOPTS"] == f"-o cache_dir=.pytest_cache-{step_name}"
            environments.append(environment)
        assert len({environment["UV_PROJECT_ENVIRONMENT"] for environment in environments}) == len(
            environments
        )
        assert len({environment["PYTEST_ADDOPTS"] for environment in environments}) == len(
            environments
        )


def test_default_python_commands_are_active_only_and_main_keeps_production_installs() -> None:
    pr_commands = _commands(_steps(_workflow("pr.yaml"))["python-checks"])
    main_commands = _commands(_steps(_workflow("main.yaml"))["python-checks"])
    assert pr_commands == _EXPECTED_PR_PYTHON_COMMANDS
    assert main_commands == (*_EXPECTED_PR_PYTHON_COMMANDS, *_MAIN_PRODUCTION_INSTALLS)
    rendered = "\n".join(pr_commands)
    for prohibited in ("hyperp-intelligence", "--group training", "large_boundary", "178328"):
        assert prohibited not in rendered


def test_query_workloads_are_active_only_have_manifest_evidence_and_pr_main_parity() -> None:
    manifests: dict[str, tuple[str, ...]] = {}
    for name in _WORKFLOWS:
        text = (_ROOT / ".woodpecker" / name).read_text(encoding="utf-8")
        manifests[name] = validate_default_query_manifest(text)
        assert text.count("ci_selection_manifest.py query-manifest") == len(_SHARDS)
        assert "services/intelligence" not in text
        for sentinel in ACTIVE_QUERY_SENTINELS:
            assert sentinel in text
    assert manifests["pr.yaml"] == manifests["main.yaml"]


def test_neo4j_services_are_isolated_and_each_has_retained_consumers() -> None:
    expected_services = {
        "neo4j-projection": "ci-projection",
        "neo4j-census-migration-api": "ci-census-migration-api",
        "neo4j-tenant-mapping": "ci-tenant-mapping",
    }
    for name in _WORKFLOWS:
        document = _workflow(name)
        services = document.get("services")
        assert isinstance(services, list)
        by_name = {
            str(service["name"]): cast(dict[str, object], service)
            for service in services
            if isinstance(service, dict)
        }
        assert set(by_name) == set(expected_services)
        for service_name, password in expected_services.items():
            service = by_name[service_name]
            assert service.get("image") == "neo4j:5.26-community"
            environment = service.get("environment")
            assert isinstance(environment, dict)
            typed = cast(dict[str, object], environment)
            assert typed["NEO4J_AUTH"] == f"neo4j/{password}"
            assert {key: value for key, value in typed.items() if key != "NEO4J_AUTH"} == (
                _NEO4J_SETTINGS
            )
        steps = _steps(document)
        projection = _environment(steps["neo4j-projection-checks"])
        assert projection["HYPERP_NEO4J_ACTIVE_PUBLICATION_TEST_SERVICE_HOST"] == (
            "neo4j-projection"
        )
        assert projection["HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST"] == (
            "neo4j-projection"
        )
        mapping = _environment(steps["neo4j-tenant-mapping-checks"])
        assert mapping["HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_SERVICE_HOST"] == (
            "neo4j-tenant-mapping"
        )
        for step_name in _SHARDS:
            commands = _commands(steps[step_name])
            assert commands[0].startswith("uv sync --frozen --group dev --package")
            assert "scripts/wait_for_neo4j.py" in "\n".join(commands)


def test_selection_manifest_is_exact_pre_import_and_defaults_new_tests_to_active() -> None:
    assert all((_ROOT / path).is_file() for path in HISTORICAL_TEST_MODULES)
    assert all((_ROOT / path).is_file() for path in HISTORICAL_SOURCE_PATHS)
    assert "services/api/tests/test_crm_deal_identity_repair_reader_classification.py" in (
        HISTORICAL_TEST_MODULES
    )
    repair_tests = {
        path.relative_to(_ROOT).as_posix()
        for path in (_ROOT / "services/ingestion/tests").glob("test_crm_deal_identity_repair*.py")
    }
    assert repair_tests - HISTORICAL_TEST_MODULES == {
        "services/ingestion/tests/test_crm_deal_identity_repair_reader_contract.py"
    }
    conftest = (_ROOT / "conftest.py").read_text(encoding="utf-8")
    assert "pytest_ignore_collect" in conftest
    assert "is_historical_test(Path(collection_path))" in conftest
    assert "glob" not in conftest and "crm*" not in conftest
    assert ACTIVE_NODE_SENTINELS == _EXPECTED_ACTIVE_NODE_SENTINELS
    assert _HISTORICAL_DORMANT_SOURCE_PATHS <= set(HISTORICAL_SOURCE_PATHS)
    assert _SHARED_ACTIVE_SOURCE_PATHS <= ACTIVE_SHARED_SOURCE_PATHS
    assert ACTIVE_SHARED_SOURCE_PATHS.isdisjoint(HISTORICAL_SOURCE_PATHS)


def test_historical_restoration_is_gated_and_restores_historical_api_reader() -> None:
    script = (_ROOT / "scripts/ci_historical_validation.py").read_text(encoding="utf-8")
    assert "--acknowledge-dormant-reactivation" in script
    assert "--disposable-neo4j" in script
    assert "HYPERP_HISTORICAL_DISPOSABLE_NEO4J" in script
    runner = runpy.run_path(str(_ROOT / "scripts/ci_historical_validation.py"))
    commands = runner["_commands"]("<historical-mypy-config>")
    assert any(
        command.environment == "ingestion"
        and command.argv == _EXPECTED_HISTORICAL_TRAINING_SYNC
        for command in commands
    )
    assert "hyperp-intelligence" in script
    assert "test_crm_deal_identity_repair_reader_classification.py" in script
    for name in _WORKFLOWS:
        workflow = (_ROOT / ".woodpecker" / name).read_text(encoding="utf-8")
        assert "ci_historical_validation.py" in workflow
        assert "--execute" not in workflow
        assert "HYPERP_HISTORICAL_REACTIVATION_ACK" not in workflow


def test_active_mypy_uses_exact_skip_overrides_without_skipping_logistic_training_types() -> None:
    document = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    overrides = document["tool"]["mypy"]["overrides"]
    active_override = next(
        override for override in overrides if override.get("follow_imports") == "skip"
    )
    expected_modules = {
        "src." + path.removeprefix("services/ingestion/src/").removesuffix(".py").replace("/", ".")
        for path in HISTORICAL_SOURCE_PATHS
    }
    assert set(active_override["module"]) == expected_modules
    assert _HISTORICAL_DORMANT_SOURCE_PATHS <= set(HISTORICAL_SOURCE_PATHS)
    assert ACTIVE_SHARED_SOURCE_PATHS.isdisjoint(HISTORICAL_SOURCE_PATHS)
    assert "src.sales_prediction.trainer.logistic" not in active_override["module"]
    assert any(
        "src.sales_prediction.trainer.logistic" in override.get("module", [])
        for override in overrides
    )
    assert all(override.get("follow_imports") != "skip" for override in overrides[:-1])


def test_workflows_retain_untrusted_execution_and_failure_suppression_bans() -> None:
    prohibited = (
        "privileged",
        "volumes",
        "docker compose",
        "docker.sock",
        "retry",
        "allow_failure",
        "ignore_failure",
        "continue-on-error",
        "|| true",
    )
    for name in _WORKFLOWS:
        rendered = (_ROOT / ".woodpecker" / name).read_text(encoding="utf-8").lower()
        for token in prohibited:
            assert token not in rendered

_EXPECTED_QUERY_TEST_PATHS = frozenset(
    {
        "services/ingestion/tests/test_active_publication_fencing_neo4j.py",
        "services/ingestion/tests/test_crm_company_membership_neo4j.py",
        "services/ingestion/tests/test_crm_deal_count_migration_neo4j.py",
        "services/ingestion/tests/test_crm_tenant_activation_neo4j.py",
        "services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_freshness_integrity.py",
        "services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_lifecycle.py",
        "services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_preparation.py",
        "services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_strictness.py",
        "services/ingestion/tests/test_crm_tenant_projection_repository_neo4j.py",
        "services/ingestion/tests/test_identifier_scope_migrations_neo4j.py",
        "services/ingestion/tests/test_identifier_scope_schema_neo4j.py",
        "services/ingestion/tests/test_identity_link_revision_baseline_neo4j.py",
        "services/ingestion/tests/test_ingestion_control_instance_migration_neo4j.py",
        "services/ingestion/tests/test_loyalty_points_migration_neo4j.py",
        "services/ingestion/tests/test_person_completeness_migration_neo4j.py",
        "services/ingestion/tests/test_standalone_crm_census_neo4j.py",
        "services/ingestion/tests/test_standalone_crm_lane_a_schema_neo4j.py",
        "services/ingestion/tests/test_standalone_crm_source_child_integration_neo4j.py",
        "services/api/tests/test_identity_link_revisions_neo4j.py",
        "services/api/tests/test_person_crm_metrics_neo4j.py",
        "services/api/tests/test_person_graph_neo4j.py",
        "services/api/tests/test_person_identifiers_neo4j.py",
        "services/api/tests/test_persons_list_neo4j_234.py",
        "services/api/tests/test_persons_list_plan_neo4j.py",
        "services/api/tests/test_persons_list_possible_match_neo4j.py",
    }
)


def _query_test_paths(commands: tuple[str, ...]) -> frozenset[str]:
    return frozenset(
        token
        for command in commands
        for token in command.split()
        if token.startswith("services/") and token.endswith(".py") and "pytest" in command
    )


def test_frontend_commands_and_complete_query_inventory_remain_exact() -> None:
    expected_frontend = {
        "pr.yaml": (
            "cd services/frontend2",
            "npm install --legacy-peer-deps",
            "npm run typecheck",
            "npx eslint src",
            "npm test",
        ),
        "main.yaml": (
            "cd services/frontend2",
            "npm install --legacy-peer-deps",
            "npm test",
            "npm run build",
        ),
    }
    for name in _WORKFLOWS:
        steps = _steps(_workflow(name))
        frontend = "frontend-checks" if name == "pr.yaml" else "frontend-build"
        assert _commands(steps[frontend]) == expected_frontend[name]
        query_commands = tuple(
            command
            for shard in _SHARDS
            for command in _commands(steps[shard])
        )
        assert _query_test_paths(query_commands) == _EXPECTED_QUERY_TEST_PATHS


def test_collection_and_query_evidence_are_ordered_and_labeled_by_kind() -> None:
    for name in _WORKFLOWS:
        steps = _steps(_workflow(name))
        python_commands = _commands(steps["python-checks"])
        for service in ("api", "ingestion"):
            collect = next(
                index
                for index, command in enumerate(python_commands)
                if f"pytest services/{service}/tests --collect-only" in command
            )
            verify = next(
                index
                for index, command in enumerate(python_commands)
                if f"verify-nodes --service {service}" in command
            )
            assert collect < verify
        for shard in _SHARDS:
            commands = _commands(steps[shard])
            query_manifest = next(
                index for index, command in enumerate(commands) if "query-manifest" in command
            )
            last_pytest = max(
                index for index, command in enumerate(commands) if " pytest " in command
            )
            assert last_pytest < query_manifest
    script = (_ROOT / "scripts/ci_selection_manifest.py").read_text(encoding="utf-8")
    assert "collected-node=" in script
    assert "configured-query=" in script
    assert "default-node=" not in script
    assert "default-query=" not in script


def test_query_connections_have_complete_families_and_readiness_precedes_execution() -> None:
    expected_families = {
        "neo4j-projection-checks": {
            "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",
            "HYPERP_NEO4J_ACTIVE_PUBLICATION_TEST",
        },
        "neo4j-census-migration-api-checks": {
            "HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST",
            "HYPERP_NEO4J_CRM_METRICS_TEST",
            "HYPERP_NEO4J_PERSON_LIST_TEST",
            "HYPERP_NEO4J_PERSON_COMPLETENESS_TEST",
            "HYPERP_NEO4J_LOYALTY_POINTS_TEST",
            "HYPERP_NEO4J_CRM_DEAL_COUNT_TEST",
            "HYPERP_NEO4J_CONTROL_MIGRATION_TEST",
            "HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST",
            "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",
        },
        "neo4j-tenant-mapping-checks": {"HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST"},
    }
    for name in _WORKFLOWS:
        steps = _steps(_workflow(name))
        for shard, families in expected_families.items():
            environment = _environment(steps[shard])
            actual = {
                key.removesuffix("_URI")
                for key in environment
                if key.startswith("HYPERP_NEO4J_") and key.endswith("_URI")
            }
            assert actual == families
            for family in families:
                for suffix in ("_URI", "_USER", "_PASSWORD", "_SERVICE_HOST"):
                    assert f"{family}{suffix}" in environment
            commands = _commands(steps[shard])
            readiness = min(
                index for index, command in enumerate(commands) if "wait_for_neo4j.py" in command
            )
            first_pytest = min(
                index for index, command in enumerate(commands) if " pytest " in command
            )
            assert readiness < first_pytest
    publication_path = (
        _ROOT / "services/ingestion/tests/test_active_publication_fencing_neo4j.py"
    )
    publication = publication_path.read_text(encoding="utf-8")
    assert publication.index("_initialize_control_schema(driver)") < publication.index(
        "yield driver"
    )


def test_historical_runner_isolated_enforces_numpy_and_requires_both_neo4j_families() -> None:
    script = (_ROOT / "scripts/ci_historical_validation.py").read_text(encoding="utf-8")
    assert "UV_PROJECT_ENVIRONMENT" in script
    assert ".venv-historical-{name}" in script
    runner = runpy.run_path(str(_ROOT / "scripts/ci_historical_validation.py"))
    commands = runner["_commands"]("<historical-mypy-config>")
    assert any(
        command.environment == "ingestion"
        and command.argv == _EXPECTED_HISTORICAL_TRAINING_SYNC
        for command in commands
    )
    assert any(
        command.require_no_skips
        and any("test_sales_prediction_logistic.py" in item for item in command.argv)
        and any("test_sales_prediction_evaluator.py" in item for item in command.argv)
        for command in commands
    )
    assert "import numpy; print(numpy.__version__)" in script
    assert "test_sales_prediction_logistic.py" in script
    assert "test_sales_prediction_evaluator.py" in script
    assert "require_no_skips=True" in script
    assert "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST_URI" in script
    assert "HYPERP_NEO4J_INTELLIGENCE_HISTORICAL_TEST_URI" in script
    assert "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST_URI" in script


def test_preimport_gate_and_behavioral_coverage_reject_explicit_historical_targets() -> None:
    gate = (_ROOT / "conftest.py").read_text(encoding="utf-8")
    behavior = (_ROOT / "services/api/tests/test_ci_selection_gate.py").read_text(
        encoding="utf-8"
    )
    assert "pytest_cmdline_main" in gate
    assert "_reject_explicit_historical_targets" in gate
    assert "active profile refuses direct historical test selection before import" in gate
    assert "test_recursive_active_collection_ignores_exact_historical_module" in behavior
    assert "test_active_profile_rejects_explicit_historical_path_and_node_before_import" in behavior
    assert "test_historical_profile_requires_acknowledgment_before_selection" in behavior
