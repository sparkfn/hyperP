"""CI-only active/historical workload selection for issue #440."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
ACTIVE_PROFILE: Final[str] = "active"
HISTORICAL_PROFILE: Final[str] = "historical"

# These are exact modules, not name prefixes. New tests are active by default.
HISTORICAL_TEST_MODULES: Final[frozenset[str]] = frozenset(
    {
        "services/api/tests/test_crm_deal_identity_repair_reader_classification.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_artifacts.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_control.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_control_cli.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_control_core.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_execution_models.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_execution_records.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_integration.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_integration_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_integration_repository.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_inventory.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_ledger_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_ledger_repository.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_mutation_classifier.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_mutation_models.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_mutation_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_mutation_repository.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_probe.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_qualification.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_reader_classification.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rebase.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rollback_models.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rollback_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rollback_repository.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_rollback_service.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_status_snapshot.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_task.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_models.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_neo4j.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_records.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_repository.py",
        "services/ingestion/tests/test_crm_deal_identity_repair_verification_secondary.py",
        "services/ingestion/tests/test_sales_prediction_evaluator.py",
        "services/ingestion/tests/test_sales_prediction_logistic.py",
    }
)

# Repair-named modules below are transitively required by retained ledger/control types.
ACTIVE_SHARED_SOURCE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "services/ingestion/src/crm_deal_identity_repair/allocation.py",
        "services/ingestion/src/crm_deal_identity_repair/approval_overlay.py",
        "services/ingestion/src/crm_deal_identity_repair/classifier.py",
        "services/ingestion/src/crm_deal_identity_repair/execution_boundary_models.py",
        "services/ingestion/src/crm_deal_identity_repair/execution_records.py",
        "services/ingestion/src/crm_deal_identity_repair/execution_status_models.py",
        "services/ingestion/src/crm_deal_identity_repair/inventory.py",
        "services/ingestion/src/crm_deal_identity_repair/models.py",
        "services/ingestion/src/crm_deal_identity_repair/mutation_models.py",
        "services/ingestion/src/crm_deal_identity_repair/qualification_inventory.py",
        "services/ingestion/src/crm_deal_identity_repair/task_inspection.py",
        "services/ingestion/src/crm_deal_identity_repair/verification_equations.py",
        "services/ingestion/src/crm_deal_identity_repair/verification_models.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_control.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_ledger.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_ledger_migration.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_ledger_records.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_control.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_integration.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_ledger.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_boundary_evidence.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_status_evidence.py",
        "services/ingestion/src/graph/crm_deal_identity_repair_status_snapshot.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair.py",
        "services/ingestion/src/graph/queries/crm_deal_identity_repair_verification.py",
    }
)

# Exclusively dormant repair/archive/model modules stay outside default source roots.
HISTORICAL_SOURCE_PATHS: Final[tuple[str, ...]] = (
    "services/ingestion/src/crm_deal_identity_repair_tasks.py",
    "services/ingestion/src/crm_deal_identity_repair_control.py",
    "services/ingestion/src/crm_deal_identity_repair/artifacts.py",
    "services/ingestion/src/crm_deal_identity_repair/cli.py",
    "services/ingestion/src/crm_deal_identity_repair/execution_protocols.py",
    "services/ingestion/src/crm_deal_identity_repair/integration_models.py",
    "services/ingestion/src/crm_deal_identity_repair/integration_runtime.py",
    "services/ingestion/src/crm_deal_identity_repair/integration_service.py",
    "services/ingestion/src/crm_deal_identity_repair/mutation_classifier.py",
    "services/ingestion/src/crm_deal_identity_repair/mutation_service.py",
    "services/ingestion/src/crm_deal_identity_repair/qualification.py",
    "services/ingestion/src/crm_deal_identity_repair/quiescence.py",
    "services/ingestion/src/crm_deal_identity_repair/rebase.py",
    "services/ingestion/src/crm_deal_identity_repair/rollback_models.py",
    "services/ingestion/src/crm_deal_identity_repair/rollback_service.py",
    "services/ingestion/src/crm_deal_identity_repair/verification_service.py",
    "services/ingestion/src/graph/crm_deal_identity_repair.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_integration.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation_errors.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation_authority.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation_payloads.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation_records.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation_state.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_mutation_structures.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rebase.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback_image.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback_ledger.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback_postcondition.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback_records.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback_restoration.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_rollback_state.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_errors.py",
    "services/ingestion/src/graph/queries/crm_deal_identity_repair_mutation.py",
    "services/ingestion/src/graph/queries/crm_deal_identity_repair_rebase.py",
    "services/ingestion/src/graph/queries/crm_deal_identity_repair_rollback.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_derived.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_pair.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_records.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_replay.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_run.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_secondary.py",
    "services/ingestion/src/graph/crm_deal_identity_repair_verification_support.py",
)

ACTIVE_NODE_SENTINELS: Final[dict[str, tuple[str, ...]]] = {
    "api": (
        "services/api/tests/test_active_api_reader_contract.py::"
        "test_api_authoritative_reader_parity_excludes_retired_links",
        "services/api/tests/test_mcp_app.py::"
        "test_mcp_tools_match_every_canonical_api_operation",
        "services/api/tests/test_ci_selection_gate.py::"
        "test_root_plugin_rejects_explicit_historical_targets_before_import",
        "services/api/tests/test_person_crm_metrics_neo4j.py::"
        "test_deal_metrics_query_uses_projected_stage_and_excludes_live_activity_records",
    ),
    "ingestion": (
        "services/ingestion/tests/test_active_publication_fencing_neo4j.py::"
        "test_publication_reservation_and_repair_claim_are_mutually_exclusive",
        "services/ingestion/tests/test_active_publication_fencing_neo4j.py::"
        "test_stale_publication_confirmation_fails_closed",
        "services/ingestion/tests/test_active_relationship_reader_contract.py::"
        "test_active_materializers_remain_classified_and_current_filtered",
        "services/ingestion/tests/test_active_reader_classifier_discovery.py::"
        "test_clause_boundary_discovery_after_create_fails_closed",
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

ACTIVE_QUERY_SENTINELS: Final[tuple[str, ...]] = (
    "test_active_publication_fencing_neo4j.py",
    "test_person_crm_metrics_neo4j.py",
    "test_standalone_crm_census_neo4j.py",
    "test_standalone_crm_source_child_integration_neo4j.py",
    "test_crm_tenant_mapping_repository_neo4j_preparation.py",
    "test_crm_tenant_projection_repository_neo4j.py",
    "test_crm_tenant_activation_neo4j.py",
)


def repository_relative(path: Path) -> str | None:
    """Return a repository-relative POSIX path when possible."""
    try:
        return path.resolve().relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return None


def is_historical_test(path: Path) -> bool:
    """Return whether an exact test module is historical-only."""
    relative = repository_relative(path)
    return relative in HISTORICAL_TEST_MODULES if relative is not None else False


def tool_exclude_args(tool: str) -> tuple[str, ...]:
    """Return exact CLI exclusions for a supported static-analysis tool."""
    if tool == "ruff":
        return tuple(f"--exclude={path}" for path in HISTORICAL_SOURCE_PATHS)
    if tool == "mypy":
        escaped = "|".join(path.replace(".", r"\.") for path in HISTORICAL_SOURCE_PATHS)
        return (f"--exclude=^(?:{escaped})$",)
    raise ValueError(f"unsupported tool: {tool}")



def validate_active_node_sentinel_definitions() -> None:
    """Require every active sentinel to resolve to an actual top-level test function."""
    for sentinels in ACTIVE_NODE_SENTINELS.values():
        for sentinel in sentinels:
            path_text, function_name = sentinel.split("::", maxsplit=1)
            path = REPOSITORY_ROOT / path_text
            if not path.is_file():
                raise ValueError(f"active sentinel source is missing: {sentinel}")
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            functions = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if function_name not in functions:
                raise ValueError(f"active sentinel function is missing: {sentinel}")

def selected_node_ids(output: str) -> tuple[str, ...]:
    """Extract pytest node IDs from quiet collection output."""
    return tuple(
        line.strip()
        for line in output.splitlines()
        if line.strip().startswith("services/") and "::" in line
    )


def validate_default_nodes(service: str, output: str) -> tuple[str, ...]:
    """Validate actual default collection against positive and negative manifest entries."""
    validate_active_node_sentinel_definitions()
    nodes = selected_node_ids(output)
    if not nodes:
        raise ValueError(f"{service} collection emitted no test nodes")
    historical = tuple(
        node for node in nodes if node.split("::", 1)[0] in HISTORICAL_TEST_MODULES
    )
    if historical:
        raise ValueError(f"historical tests leaked into default collection: {historical}")
    missing = tuple(sentinel for sentinel in ACTIVE_NODE_SENTINELS[service] if not any(
        node.startswith(sentinel) for node in nodes
    ))
    if missing:
        raise ValueError(f"default collection lost active sentinels: {missing}")
    return nodes


def query_commands(workflow_text: str) -> tuple[str, ...]:
    """Return pytest commands from a Woodpecker workflow without YAML dependencies."""
    return tuple(
        line.strip()[2:]
        for line in workflow_text.splitlines()
        if line.strip().startswith("- ") and " pytest " in line
    )


def validate_default_query_manifest(workflow_text: str) -> tuple[str, ...]:
    """Reject dormant query invocations and require retained query sentinels."""
    commands = query_commands(workflow_text)
    rendered = "\n".join(commands)
    prohibited = (
        "hyperp-intelligence",
        "--group training",
        "large_boundary",
        "178328",
        "178,328",
        "test_crm_deal_identity_repair_ledger_neo4j.py",
        "test_crm_deal_identity_repair_mutation_neo4j.py",
        "test_crm_deal_identity_repair_verification_neo4j.py",
        "test_crm_deal_identity_repair_rollback_neo4j.py",
        "test_crm_deal_identity_repair_integration_neo4j.py",
    )
    leaked = tuple(token for token in prohibited if token in rendered)
    if leaked:
        raise ValueError(f"default query manifest contains dormant workload: {leaked}")
    missing = tuple(sentinel for sentinel in ACTIVE_QUERY_SENTINELS if sentinel not in rendered)
    if missing:
        raise ValueError(f"default query manifest lost active sentinels: {missing}")
    return commands
