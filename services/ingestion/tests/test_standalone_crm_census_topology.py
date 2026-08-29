"""Standalone CRM census topology and compatibility contracts for issue #273."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast, get_args

import httpx
import pytest
from src import main, tasks
from src.celery_app import INGESTION_QUEUE, celery_app
from src.connectors.bitrix_openlines.client import BitrixHttpCallIntent, BitrixOpenLinesClient
from src.graph.queries.ingestion_control_instance_migration import (
    MIGRATION_KEY as CONTROL_MIGRATION_KEY,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_migration import MIGRATION_KEY as CENSUS_MIGRATION_KEY
from src.graph.standalone_crm_lane_a_migration import MIGRATION_KEY as LANE_A_MIGRATION_KEY
from src.ingestion_config import (
    BitrixOpenLinesConfig,
    bitrix_configuration_digest,
    bitrix_legacy_explicit_category_digest,
)
from src.standalone_crm_census_authority import UnavailableStandaloneCrmCensusAuthority
from src.standalone_crm_census_models import StandaloneCrmCensusRequest
from src.standalone_crm_census_tasks import (
    _source_child_runtime,
    cancel_standalone_crm_census,
    reconcile_standalone_crm_census,
    recover_standalone_crm_publication,
    resume_standalone_crm_census,
    run_standalone_crm_census_unit,
    start_standalone_crm_census,
)
from src.tasks import LegacyBitrixExecutionStream

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_CENSUS_TASK_MODULE = "src.standalone_crm_census_tasks"
_CENSUS_TASK_NAMES = {
    "src.standalone_crm_census_tasks.reconcile_standalone_crm_census",
    "src.standalone_crm_census_tasks.recover_standalone_crm_publication",
}
_LEGACY_CATEGORY_DIGEST = "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
_LEGACY_RUNTIME_DIGEST = "sha256:a449c56111af4eff4d8d3182355d037bee51760c45fc77c1451b4cac5bb4e75a"


def test_census_tasks_are_registered_and_routed_with_the_closed_source_child_task() -> None:
    assert _CENSUS_TASK_MODULE in celery_app.conf.include
    assert _CENSUS_TASK_NAMES <= set(celery_app.tasks)
    assert {
        task.name
        for task in (
            start_standalone_crm_census,
            reconcile_standalone_crm_census,
            cancel_standalone_crm_census,
            resume_standalone_crm_census,
            recover_standalone_crm_publication,
        )
    } <= set(celery_app.tasks)
    assert reconcile_standalone_crm_census.name in _CENSUS_TASK_NAMES
    assert recover_standalone_crm_publication.name in _CENSUS_TASK_NAMES
    assert celery_app.conf.task_routes[_CENSUS_TASK_MODULE + ".*"] == {"queue": INGESTION_QUEUE}
    assert run_standalone_crm_census_unit.name in celery_app.tasks
    assert "bitrix_crm_identity" not in celery_app.conf.task_routes
    assert "src.tasks.run_ingestion_task" in celery_app.conf.task_routes


def test_census_tasks_are_manual_only_and_fail_closed_until_child_handlers_exist() -> None:
    schedule = celery_app.conf.beat_schedule

    assert all("standalone_crm" not in name for name in schedule)
    assert all("standalone_crm" not in str(entry) for entry in schedule.values())
    task_source = inspect.getsource(reconcile_standalone_crm_census) + inspect.getsource(
        recover_standalone_crm_publication
    )
    assert "BitrixOpenLinesClient" not in task_source
    assert "run_ingestion" not in task_source


def test_source_child_task_has_real_closed_runtime_wiring_not_a_production_stub() -> None:
    runtime_source = inspect.getsource(_source_child_runtime)
    task_source = inspect.getsource(run_standalone_crm_census_unit)

    assert "StandaloneCrmContactSourceHandler" in runtime_source
    assert "StandaloneCrmLeadSourceHandler" in runtime_source
    assert "StandaloneCrmCompanySourceHandler" in runtime_source
    assert "StandaloneCrmSourceChildBitrixSessionFactory" in runtime_source
    assert "RuntimeError" not in runtime_source
    assert "_source_child_runtime()" in task_source


def test_default_off_configuration_and_authority_admission_fail_closed() -> None:
    config = BitrixOpenLinesConfig()
    repository_admit = inspect.getsource(StandaloneCrmCensusRepository.admit)

    assert config.standalone_crm_identity_enabled is False
    assert config.standalone_crm_identity_schedule_enabled is False
    with pytest.raises(RuntimeError, match="authority is unavailable; refusing mutation"):
        UnavailableStandaloneCrmCensusAuthority().verify(cast(StandaloneCrmCensusRequest, None))
    assert (
        repository_admit.index("assert_standalone_crm_census_ready")
        < repository_admit.index("BitrixSourceInstanceRepository(self._client).admit")
        < repository_admit.index("self._client.execute_write")
    )


def test_direct_standalone_aliases_remain_blocked_before_runtime_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "initialize_ingestion_graph",
        lambda: pytest.fail("standalone aliases must fail before graph initialization"),
    )
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: pytest.fail("standalone aliases must fail before settings access"),
    )

    for mode in ("api", "batch", "backfill"):
        with pytest.raises(
            main.StandaloneCrmCensusContextRequiredError, match="frozen census child"
        ):
            main.get_connector("bitrix_crm_identity", mode=mode)
    with pytest.raises(main.StandaloneCrmCensusContextRequiredError, match="frozen census child"):
        main.run_ingestion("bitrix_crm_identity", mode="api")


def test_no_api_mcp_or_domain_writer_is_exposed_for_census_control() -> None:
    api_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (_REPOSITORY_ROOT / "services" / "api" / "src").rglob("*.py")
    )
    control_source = (
        _REPOSITORY_ROOT / "services" / "ingestion" / "src" / "standalone_crm_census_control.py"
    ).read_text(encoding="utf-8")

    assert "standalone_crm_census" not in api_source
    assert "StandaloneCrmCensus" not in api_source
    assert "execute_write" not in control_source
    assert ".delay(" not in control_source
    assert ".apply_async(" not in control_source


def test_lane_a_contracts_add_no_task_route_schedule_source_call_or_concrete_writer() -> None:
    contract_paths = (
        "standalone_crm_child_contracts.py",
        "standalone_crm_unit_repository.py",
        "crm_company_contract_primitives.py",
        "crm_company_contracts.py",
        "crm_tenant_mapping_contracts.py",
        "crm_tenant_projection_contracts.py",
        "crm_tenant_projection_records.py",
        "crm_tenant_projection_release_contracts.py",
    )
    contract_source = "\\n".join(
        (_REPOSITORY_ROOT / "services" / "ingestion" / "src" / path).read_text(encoding="utf-8")
        for path in contract_paths
    )
    task_names = set(celery_app.tasks)
    task_routes = set(celery_app.conf.task_routes)

    assert run_standalone_crm_census_unit.name in task_names
    assert "src.standalone_crm_census_tasks.*" in task_routes
    assert "BitrixOpenLinesClient" not in contract_source
    assert ".delay(" not in contract_source
    assert ".apply_async(" not in contract_source
    assert "Neo4jClient" not in contract_source
    assert "execute_write" not in contract_source
    assert "head swap" not in contract_source.lower()


def test_272_273_identities_and_direct_path_gates_remain_distinct_from_lane_a() -> None:
    main_source = (_REPOSITORY_ROOT / "services" / "ingestion" / "src" / "main.py").read_text(
        encoding="utf-8"
    )

    assert CONTROL_MIGRATION_KEY == "bitrix_control_instance_v1"
    assert CENSUS_MIGRATION_KEY == "standalone_crm_census_control_v1"
    assert LANE_A_MIGRATION_KEY == "standalone_crm_lane_a_contracts_v1"
    assert len({CONTROL_MIGRATION_KEY, CENSUS_MIGRATION_KEY, LANE_A_MIGRATION_KEY}) == 3
    assert main_source.index("assert_ingestion_control_ready(client)") < main_source.index(
        "ensure_standalone_crm_census_ready(client)"
    )
    assert main_source.index("ensure_standalone_crm_census_ready(client)") < main_source.index(
        "ensure_standalone_crm_lane_a_ready(client)"
    )


def test_legacy_bitrix_stream_literals_digests_and_task_shape_are_unchanged() -> None:
    categories = ("2", "7", "8")
    legacy_config = BitrixOpenLinesConfig()
    scoped_config = BitrixOpenLinesConfig(
        included_crm_category_ids=list(categories),
        entity_by_crm_category_id={"2": "eko", "7": "fundbox", "8": "speedzone"},
    )

    assert get_args(main.BitrixExecutionStream) == (
        "legacy",
        "crm_deals",
        "crm_activities",
        "openlines_conversations",
    )
    assert get_args(LegacyBitrixExecutionStream) == (
        "crm_deals",
        "crm_activities",
        "openlines_conversations",
    )
    assert tasks.run_ingestion_task.name == "src.tasks.run_ingestion_task"
    assert tuple(inspect.signature(tasks.run_ingestion_task.run).parameters)[:3] == (
        "source_key",
        "mode",
        "dump_path",
    )
    assert bitrix_configuration_digest(legacy_config, categories) == _LEGACY_CATEGORY_DIGEST
    assert (
        bitrix_legacy_explicit_category_digest(scoped_config, categories) == _LEGACY_CATEGORY_DIGEST
    )
    assert bitrix_configuration_digest(scoped_config, categories) == _LEGACY_RUNTIME_DIGEST
    assert (
        bitrix_configuration_digest(
            BitrixOpenLinesConfig(
                standalone_crm_identity_enabled=True,
                standalone_crm_identity_schedule_enabled=True,
            ),
            categories,
        )
        == _LEGACY_CATEGORY_DIGEST
    )


def test_optional_http_reservation_hook_is_backward_compatible_when_absent() -> None:
    http = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=http,
    )
    try:
        assert (
            inspect.signature(BitrixOpenLinesClient).parameters["reservation_hook"].default is None
        )
        assert client._reservation_hook is None  # noqa: SLF001 - compatibility boundary
        client._record_reservation_outcome(  # noqa: SLF001 - no-hook must be a no-op
            BitrixHttpCallIntent("intent-1", "crm.contact.list", 0),
            "succeeded",
        )
    finally:
        client.close()


def test_both_woodpecker_workflows_must_run_the_standalone_crm_neo4j_suites() -> None:
    expected_suites = (
        "services/ingestion/tests/test_standalone_crm_census_neo4j.py",
        "services/ingestion/tests/test_standalone_crm_lane_a_schema_neo4j.py",
    )

    for workflow_name in ("pr.yaml", "main.yaml"):
        workflow = (_REPOSITORY_ROOT / ".woodpecker" / workflow_name).read_text(encoding="utf-8")
        assert all(expected_suite in workflow for expected_suite in expected_suites)
