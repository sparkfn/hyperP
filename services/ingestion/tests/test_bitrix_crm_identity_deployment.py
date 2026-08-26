"""Deployment gates for standalone Bitrix CRM identity ingestion."""

from __future__ import annotations

from typing import cast

import pytest
from src import main
from src.connectors import bitrix_crm
from src.ingestion_config import BitrixOpenLinesConfig, IngestionConfig


def test_operator_dispatched_identity_ingestion_is_fail_closed_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "get_ingestion_config", lambda: IngestionConfig())

    with pytest.raises(main.StandaloneCrmCensusContextRequiredError, match="frozen census child"):
        main.get_connector("bitrix_crm_identity", mode="api")


def test_direct_identity_alias_is_fail_closed_even_when_globally_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "get_ingestion_config",
        lambda: IngestionConfig(
            bitrix_openlines=BitrixOpenLinesConfig(
                source_instance_id="bitrix-primary",
                standalone_crm_identity_enabled=True,
            )
        ),
    )

    with pytest.raises(main.StandaloneCrmCensusContextRequiredError, match="frozen census child"):
        main.get_connector("bitrix_crm_identity", mode="api", incremental=False)


def test_unpublished_identity_child_stream_is_not_dispatchable() -> None:
    with pytest.raises(ValueError, match="Unsupported Bitrix execution stream"):
        main.get_connector(
            "bitrix_chat",
            mode="api",
            bitrix_execution_stream=cast(main.BitrixExecutionStream, "crm_contacts"),
        )


def test_identity_source_key_is_fail_closed_in_legacy_batch_mode() -> None:
    with pytest.raises(main.StandaloneCrmCensusContextRequiredError):
        main.get_connector("bitrix_crm_identity", mode="batch")


def test_bounded_identity_reader_is_not_publicly_exported_before_census_dispatch() -> None:
    assert not hasattr(bitrix_crm, "BitrixCrmIdentityConnector")
    assert not hasattr(bitrix_crm, "BitrixCrmIdentityKeysetConnector")


def test_direct_run_fails_before_graph_or_client_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main,
        "initialize_ingestion_graph",
        lambda: pytest.fail("graph initialization must not run"),
    )
    monkeypatch.setattr(main, "get_settings", lambda: pytest.fail("settings must not be read"))

    with pytest.raises(main.StandaloneCrmCensusContextRequiredError):
        main.run_ingestion("bitrix_crm_identity", mode="api")


def test_raw_task_fails_before_locks_graph_or_client_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import tasks

    monkeypatch.setattr(
        tasks,
        "_initialize_graph_under_lock",
        lambda source: pytest.fail(f"graph initialization must not run for {source}"),
    )
    monkeypatch.setattr(
        tasks,
        "_source_lock_keys",
        lambda source, mode, entity: pytest.fail(
            f"locks must not be acquired for {source}:{mode}:{entity}"
        ),
    )

    with pytest.raises(main.StandaloneCrmCensusContextRequiredError):
        tasks.run_ingestion_task.run("bitrix_crm_identity", "api")
