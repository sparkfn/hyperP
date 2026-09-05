"""Parsed Woodpecker Neo4j shard topology contract for issue #392."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import cast

import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_SHARD_STEP_NAME = re.compile(r"neo4j-[a-z0-9-]+-checks")

_NEO4J_SHARDS = {
    "projection": {
        "service": "neo4j-projection",
        "families": ("HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",),
        "readiness_family": "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",
    },
    "ledger-310": {
        "service": "neo4j-ledger-310",
        "families": ("HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST",),
        "readiness_family": "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST",
    },
    "census-migration-api": {
        "service": "neo4j-census-migration-api",
        "families": (
            "HYPERP_NEO4J_PERSON_IDENTIFIERS_TEST",
            "HYPERP_NEO4J_CRM_METRICS_TEST",
            "HYPERP_NEO4J_PERSON_LIST_TEST",
            "HYPERP_NEO4J_PERSON_COMPLETENESS_TEST",
            "HYPERP_NEO4J_LOYALTY_POINTS_TEST",
            "HYPERP_NEO4J_CRM_DEAL_COUNT_TEST",
            "HYPERP_NEO4J_CONTROL_MIGRATION_TEST",
            "HYPERP_NEO4J_STANDALONE_CRM_CENSUS_TEST",
            "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",
        ),
        "readiness_family": "HYPERP_NEO4J_PERSON_COMPLETENESS_TEST",
    },
    "repair-mapping": {
        "service": "neo4j-repair-mapping",
        "families": (
            "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",
            "HYPERP_NEO4J_CRM_REPAIR_LEDGER_TEST",
        ),
        "readiness_family": "HYPERP_NEO4J_STANDALONE_CRM_LANE_A_TEST",
    },
}
_NEO4J_SUITE_MANIFEST = frozenset(
    {
        ("services/api/tests/test_person_identifiers_neo4j.py", ""),
        ("services/api/tests/test_person_crm_metrics_neo4j.py", ""),
        ("services/api/tests/test_persons_list_neo4j_234.py", ""),
        ("services/api/tests/test_persons_list_possible_match_neo4j.py", ""),
        ("services/api/tests/test_persons_list_plan_neo4j.py", ""),
        ("services/api/tests/test_identity_link_revisions_neo4j.py", ""),
        ("services/ingestion/tests/test_person_completeness_migration_neo4j.py", ""),
        ("services/ingestion/tests/test_loyalty_points_migration_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_deal_count_migration_neo4j.py", ""),
        ("services/ingestion/tests/test_ingestion_control_instance_migration_neo4j.py", ""),
        ("services/ingestion/tests/test_standalone_crm_census_neo4j.py", ""),
        ("services/ingestion/tests/test_standalone_crm_lane_a_schema_neo4j.py", ""),
        ("services/ingestion/tests/test_standalone_crm_source_child_integration_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_company_membership_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_preparation.py", ""),
        ("services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_lifecycle.py", ""),
        (
            "services/ingestion/tests/"
            "test_crm_tenant_mapping_repository_neo4j_freshness_integrity.py",
            "",
        ),
        ("services/ingestion/tests/test_crm_tenant_mapping_repository_neo4j_strictness.py", ""),
        ("services/ingestion/tests/test_crm_tenant_projection_repository_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_tenant_activation_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py", "test_310_"),
        ("services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py", "not test_310_"),
        ("services/ingestion/tests/test_crm_deal_identity_repair_mutation_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_deal_identity_repair_verification_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_deal_identity_repair_rollback_neo4j.py", ""),
        ("services/ingestion/tests/test_crm_deal_identity_repair_integration_neo4j.py", ""),
    }
)


def _workflow_document(workflow_name: str) -> dict[str, object]:
    raw_document = yaml.safe_load(
        (_REPOSITORY_ROOT / ".woodpecker" / workflow_name).read_text(encoding="utf-8")
    )
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
        assert isinstance(name, str)
        steps[name] = step
    return steps


def _readiness_command(family: str) -> str:
    assert (_REPOSITORY_ROOT / "scripts" / "wait_for_neo4j.py").is_file()
    return (
        "uv run --package profile-unifier-ingestion python scripts/wait_for_neo4j.py "
        f"--uri-env {family}_URI --user-env {family}_USER "
        f"--password-env {family}_PASSWORD --timeout-seconds 90"
    )


def _assert_readiness_precedes_pytest(step: dict[str, object], family: str) -> None:
    commands = step.get("commands")
    assert isinstance(commands, list)
    rendered_commands = [command for command in commands if isinstance(command, str)]
    assert len(rendered_commands) == len(commands)
    assert rendered_commands[:2] == ["uv sync --frozen", _readiness_command(family)]
    assert rendered_commands.count(_readiness_command(family)) == 1
    pytest_indexes = [
        index for index, command in enumerate(rendered_commands) if " pytest " in command
    ]
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


def test_woodpecker_neo4j_shards_are_complete_isolated_and_parity_checked() -> None:
    workflows = {name: _workflow_document(name) for name in ("pr.yaml", "main.yaml")}
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
        shard_step_names = {f"neo4j-{shard}-checks" for shard in _NEO4J_SHARDS}
        actual_shard_step_names = {
            step_name for step_name in steps if _SHARD_STEP_NAME.fullmatch(step_name)
        }
        assert actual_shard_step_names == shard_step_names
        assert len(service_by_name) == len(_NEO4J_SHARDS)
        passwords: set[str] = set()
        environments: list[dict[str, object]] = []

        for shard, contract in _NEO4J_SHARDS.items():
            service_name = contract["service"]
            service = service_by_name[service_name]
            environment = service.get("environment")
            assert service.get("image") == "neo4j:5.26-community"
            assert isinstance(environment, dict)
            service_environment = cast(dict[str, object], environment)
            assert service_environment.get("NEO4J_PLUGINS") == "[]"
            assert service_environment.get("NEO4J_server_memory_heap_initial__size") == "128m"
            assert service_environment.get("NEO4J_server_memory_heap_max__size") == "384m"
            assert service_environment.get("NEO4J_server_memory_pagecache_size") == "128m"
            auth = service_environment.get("NEO4J_AUTH")
            assert isinstance(auth, str) and auth.startswith("neo4j/")
            password = auth.removeprefix("neo4j/")
            assert password and password not in passwords
            passwords.add(password)

            step = steps[f"neo4j-{shard}-checks"]
            step_environment = step.get("environment")
            assert step.get("depends_on") == []
            readiness_family = contract["readiness_family"]
            assert isinstance(readiness_family, str)
            _assert_readiness_precedes_pytest(step, readiness_family)
            assert isinstance(step_environment, dict)
            shard_environment = cast(dict[str, object], step_environment)
            environments.append(shard_environment)
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
                for family in contract["families"]
                for suffix in ("URI", "USER", "PASSWORD", "SERVICE_HOST")
            }
            if shard == "census-migration-api":
                expected_neo4j_keys.add("HYPERP_NEO4J_PERSON_LIST_TEST_ALLOW_SCHEMA_MUTATION")
            actual_neo4j_keys = {
                key for key in shard_environment if key.startswith("HYPERP_NEO4J_")
            }
            assert actual_neo4j_keys == expected_neo4j_keys
            for family in contract["families"]:
                assert shard_environment.get(f"{family}_URI") == f"bolt://{service_name}:7687"
                assert shard_environment.get(f"{family}_USER") == "neo4j"
                assert shard_environment.get(f"{family}_PASSWORD") == password
                assert shard_environment.get(f"{family}_SERVICE_HOST") == service_name

            family_keys = {
                key.removesuffix("_SERVICE_HOST")
                for key in shard_environment
                if key.endswith("_SERVICE_HOST") and key.startswith("HYPERP_NEO4J_")
            }
            assert family_keys == set(contract["families"])
            if workflow_name == "pr.yaml":
                assert step.get("when") == [
                    {"path": {"exclude": ["docs/**", "services/frontend2/**"]}}
                ]
            else:
                assert "when" not in step

        assert len({environment["UV_PROJECT_ENVIRONMENT"] for environment in environments}) == 4
        assert len({environment["PYTEST_ADDOPTS"] for environment in environments}) == 4
        manifests[workflow_name] = _neo4j_manifest(steps)
        rendered = str(workflow).lower()
        prohibited_terms = (
            "privileged",
            "volumes",
            "docker compose",
            "pytest-xdist",
            "xdist",
            "docker.sock",
        )
        for prohibited in prohibited_terms:
            assert prohibited not in rendered

    assert manifests["pr.yaml"] == _NEO4J_SUITE_MANIFEST
    assert manifests["main.yaml"] == _NEO4J_SUITE_MANIFEST
    assert manifests["pr.yaml"] == manifests["main.yaml"]
    assert len({path for path, _selector in manifests["pr.yaml"]}) == 25
