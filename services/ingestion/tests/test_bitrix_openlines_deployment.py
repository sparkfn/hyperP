from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest import MonkeyPatch
from src import main
from src.connectors.base import SourceConnector
from src.models import JsonValue


class StubConnector(SourceConnector):
    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        return iter(())


def test_connector_factory_supports_api_and_backfill(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []
    connector = StubConnector()
    monkeypatch.setattr(
        main,
        "create_bitrix_openlines_connector",
        lambda mode: calls.append(mode) or connector,
    )

    assert main.get_connector("bitrix_chat", mode="api") is connector
    assert main.get_connector("bitrix_chat", mode="backfill") is connector
    assert calls == ["api", "backfill"]


def test_connector_factory_preserves_bitrix_chat_batch_connector() -> None:
    connector = main.get_connector("bitrix_chat", mode="batch")

    assert isinstance(connector, main.BitrixChatConnector)


def test_connector_factory_rejects_backfill_for_other_sources() -> None:
    with pytest.raises(ValueError, match="Backfill mode is not supported"):
        main.get_connector("fundbox", mode="backfill")


def test_deployment_examples_forward_bitrix_openlines_api_configuration() -> None:
    root = Path(__file__).parents[3]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    root_env = (root / ".env.example").read_text(encoding="utf-8")
    ingestion_env = (root / "services/ingestion/.env.example").read_text(encoding="utf-8")
    names = (
        "BITRIX_OPENLINES_API_BASE_URL",
        "BITRIX_OPENLINES_API_TIMEOUT_SECONDS",
        "BITRIX_OPENLINES_API_MAX_ATTEMPTS",
        "BITRIX_OPENLINES_API_REQUEST_DELAY_SECONDS",
    )

    for name in names:
        assert f"{name}: ${{{name}" in compose
        assert f"{name}=" in root_env
        assert f"{name}=" in ingestion_env
