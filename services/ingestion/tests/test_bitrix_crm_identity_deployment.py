"""Deployment gates for standalone Bitrix CRM identity ingestion."""

from __future__ import annotations

import pytest
from src import main
from src.ingestion_config import BitrixOpenLinesConfig, IngestionConfig


def test_operator_dispatched_identity_ingestion_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "get_ingestion_config", lambda: IngestionConfig())

    with pytest.raises(PermissionError, match="identity ingestion is disabled"):
        main.get_connector("bitrix_crm_identity", mode="api")


def test_operator_dispatched_identity_ingestion_requires_explicit_config_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = object()
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
    monkeypatch.setattr(main, "create_bitrix_crm_identity_connector", lambda: expected)

    assert main.get_connector("bitrix_crm_identity", mode="api", incremental=False) is expected


def test_identity_ingestion_rejects_incremental_mode_until_checkpointed(
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

    with pytest.raises(ValueError, match="requires incremental=False"):
        main.get_connector("bitrix_crm_identity", mode="api")


def test_identity_source_key_is_not_available_in_legacy_batch_mode() -> None:
    with pytest.raises(ValueError, match="Unknown source key"):
        main.get_connector("bitrix_crm_identity", mode="batch")
