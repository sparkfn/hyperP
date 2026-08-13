from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from pytest import MonkeyPatch
from src import main
from src.connectors.base import SourceConnector
from src.connectors.bitrix import BitrixChatConnector
from src.graph.incremental_checkpoints import Neo4jCheckpointRedis
from src.models import JsonValue


class StubConnector(SourceConnector):
    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        return iter(())


def test_connector_factory_supports_api_and_backfill(monkeypatch: MonkeyPatch) -> None:
    calls: list[str] = []
    connector = StubConnector()

    def create_connector(mode: str, *, incremental: bool = True) -> SourceConnector:
        del incremental
        calls.append(mode)
        return connector

    monkeypatch.setattr(
        main,
        "create_bitrix_openlines_connector",
        create_connector,
    )

    assert main.get_connector("bitrix_chat", mode="api") is connector
    assert main.get_connector("bitrix_chat", mode="backfill") is connector
    assert calls == ["api", "backfill"]


def test_connector_factory_preserves_bitrix_chat_batch_connector() -> None:
    connector = main.get_connector("bitrix_chat", mode="batch")

    assert isinstance(connector, BitrixChatConnector)


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


def test_connector_factory_selects_dormant_bitrix_crm_streams(
    monkeypatch: MonkeyPatch,
) -> None:
    deal_connector = StubConnector()
    activity_connector = StubConnector()
    captured: dict[str, dict[str, object]] = {}

    def create_deal(**parameters: object) -> StubConnector:
        captured["deal"] = parameters
        return deal_connector

    def create_activity(**parameters: object) -> StubConnector:
        captured["activity"] = parameters
        return activity_connector

    monkeypatch.setattr(main, "create_bitrix_crm_deal_connector", create_deal)
    monkeypatch.setattr(main, "create_bitrix_crm_activity_connector", create_activity)

    assert (
        main.get_connector(
            "bitrix_chat",
            mode="api",
            bitrix_execution_stream="crm_deals",
            bitrix_source_window={
                "upper_deal_id": "900",
                "included_category_digest": "sha256:categories",
                "owner_artifact_id": None,
            },
            bitrix_max_calls=100,
            bitrix_deadline_monotonic=200.0,
        )
        is deal_connector
    )
    assert (
        main.get_connector(
            "bitrix_chat",
            mode="api",
            bitrix_execution_stream="crm_activities",
            bitrix_source_window={
                "upper_activity_id": "1200",
                "owner_artifact_id": None,
            },
            bitrix_max_calls=101,
            bitrix_deadline_monotonic=201.0,
        )
        is activity_connector
    )
    assert captured["deal"] == {
        "upper_deal_id": 900,
        "last_deal_id": None,
        "max_request_count": 100,
        "deadline_monotonic": 200.0,
    }
    assert captured["activity"] == {
        "upper_activity_id": 1200,
        "last_activity_id": None,
        "max_request_count": 101,
        "deadline_monotonic": 201.0,
    }


def test_connector_factory_uses_conversation_only_legacy_mode(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    connector = StubConnector()

    def create(
        mode: str,
        *,
        incremental: bool = True,
        checkpoint_store: object | None = None,
        include_crm_records: bool = True,
    ) -> StubConnector:
        captured.update(
            mode=mode,
            incremental=incremental,
            checkpoint_store=checkpoint_store,
            include_crm_records=include_crm_records,
        )
        return connector

    monkeypatch.setattr(main, "create_bitrix_openlines_connector", create)

    assert (
        main.get_connector(
            "bitrix_chat",
            mode="api",
            bitrix_execution_stream="openlines_conversations",
        )
        is connector
    )
    assert captured == {
        "mode": "api",
        "incremental": True,
        "checkpoint_store": None,
        "include_crm_records": False,
    }


def test_split_openlines_backfill_accepts_a_fence_aware_checkpoint_store(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    connector = StubConnector()
    checkpoint_store = object()

    def create(
        mode: str,
        *,
        incremental: bool = True,
        checkpoint_store: object | None = None,
        include_crm_records: bool = True,
    ) -> StubConnector:
        captured.update(
            mode=mode,
            incremental=incremental,
            checkpoint_store=checkpoint_store,
            include_crm_records=include_crm_records,
        )
        return connector

    monkeypatch.setattr(main, "create_bitrix_openlines_connector", create)

    assert (
        main.get_connector(
            "bitrix_chat",
            mode="backfill",
            checkpoint_store=cast(Neo4jCheckpointRedis, checkpoint_store),
            bitrix_execution_stream="openlines_conversations",
        )
        is connector
    )
    assert captured["checkpoint_store"] is checkpoint_store
    assert captured["include_crm_records"] is False


@pytest.mark.parametrize(
    ("source_key", "mode", "stream", "message"),
    [
        ("fundbox", "api", "crm_deals", "only valid for bitrix_chat"),
        ("bitrix_chat", "batch", "crm_deals", "requires bitrix_chat API or backfill"),
    ],
)
def test_connector_factory_rejects_invalid_bitrix_stream_context(
    source_key: str,
    mode: str,
    stream: main.BitrixExecutionStream,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        main.get_connector(
            source_key,
            mode=mode,
            bitrix_execution_stream=stream,
        )
