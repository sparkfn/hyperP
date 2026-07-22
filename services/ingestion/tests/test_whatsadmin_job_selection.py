from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from _test_helpers import NullContext
from pydantic import SecretStr
from pytest import MonkeyPatch
from src import main
from src.connectors.base import SourceConnector
from src.connectors.whatsadmin_api.credentials import WhatsAdminCredential, WhatsAdminEntity
from src.connectors.whatsadmin_api.models import ChatPage, SessionRow
from src.models import JsonValue


class StubConnector(SourceConnector):
    def get_source_key(self) -> str:
        return "whatsapp_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        return iter(())


def test_connector_selection_forwards_optional_entity_key(monkeypatch: MonkeyPatch) -> None:
    connector = StubConnector()
    calls: list[str | None] = []
    monkeypatch.setattr(
        main,
        "create_whatsadmin_api_connector",
        lambda entity_key=None: calls.append(entity_key) or connector,
    )

    assert main.get_connector("whatsapp_chat", mode="api", entity_key="eko") is connector
    assert main.get_connector("whatsapp_chat", mode="api") is connector
    assert calls == ["eko", None]


def test_entity_key_is_rejected_for_other_extraction_jobs() -> None:
    with pytest.raises(ValueError, match="entity_key.*whatsapp_chat"):
        main.get_connector("eko_phppos", mode="api", entity_key="eko")


@dataclass(frozen=True)
class StubSettings:
    whatsadmin_api_base_url: str = "https://whatsadmin.test"
    whatsadmin_eko_api_key: SecretStr = field(default_factory=lambda: SecretStr("hk_eko_test"))
    whatsadmin_speedzone_api_key: SecretStr = field(
        default_factory=lambda: SecretStr("hk_speedzone_test")
    )
    whatsadmin_api_page_size: int = 50
    whatsadmin_api_timeout_seconds: float = 30.0
    whatsadmin_api_max_attempts: int = 3
    whatsadmin_api_retry_base_delay_seconds: float = 1.0
    whatsadmin_legacy_entity: WhatsAdminEntity | None = "speedzone"
    celery_broker_url: str = "redis://test/0"


class StubClient:
    def __init__(self, credential: WhatsAdminCredential) -> None:
        self.entity_key = credential.entity_key

    def iter_sessions(self) -> Iterator[SessionRow]:
        return iter(())

    def iter_chat_pages(
        self,
        session_id: str,
        changed_since: str | None,
    ) -> Iterator[ChatPage]:
        _ = session_id, changed_since
        return iter(())

    def close(self) -> None:
        pass


class StubRedis:
    def get(self, name: str) -> object:
        _ = name
        return None

    def set(self, name: str, value: str) -> object:
        _ = name, value
        return None

    def delete(self, *names: str) -> object:
        _ = names
        return None

    def close(self) -> None:
        pass


def test_default_connector_factory_builds_both_entity_clients(monkeypatch: MonkeyPatch) -> None:
    captured: list[WhatsAdminEntity] = []

    def build_client(
        *,
        credential: WhatsAdminCredential,
        page_size: int,
        timeout_seconds: float,
        max_attempts: int,
        retry_base_delay_seconds: float,
    ) -> StubClient:
        assert page_size == 50
        assert timeout_seconds == 30.0
        assert max_attempts == 3
        assert retry_base_delay_seconds == 1.0
        captured.append(credential.entity_key)
        return StubClient(credential)

    monkeypatch.setattr(main, "get_settings", StubSettings)
    monkeypatch.setattr(main, "WhatsAdminApiClient", build_client)
    monkeypatch.setattr(main.Redis, "from_url", lambda *_args, **_kwargs: StubRedis())

    connector = main.create_whatsadmin_api_connector()

    assert isinstance(connector, main.WhatsAdminChatApiConnector)
    assert captured == ["eko", "speedzone"]
    assert connector._legacy_entity == "speedzone"
    assert connector._watermark._legacy_entity == "speedzone"


def test_single_entity_connector_factory_builds_only_requested_client(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[WhatsAdminEntity] = []

    def build_client(
        *,
        credential: WhatsAdminCredential,
        page_size: int,
        timeout_seconds: float,
        max_attempts: int,
        retry_base_delay_seconds: float,
    ) -> StubClient:
        _ = page_size, timeout_seconds, max_attempts, retry_base_delay_seconds
        captured.append(credential.entity_key)
        return StubClient(credential)

    monkeypatch.setattr(main, "get_settings", StubSettings)
    monkeypatch.setattr(main, "WhatsAdminApiClient", build_client)
    monkeypatch.setattr(main.Redis, "from_url", lambda *_args, **_kwargs: StubRedis())

    main.create_whatsadmin_api_connector("speedzone")

    assert captured == ["speedzone"]


@dataclass(frozen=True)
class TaskSettings:
    log_level: str = "INFO"
    max_concurrent_ingestions: int = 1


def test_celery_task_forwards_entity_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    calls: list[tuple[str, str, str | None, str | None, bool]] = []
    monkeypatch.setattr(tasks, "setup_logging", lambda _level: None)
    monkeypatch.setattr(tasks, "get_settings", TaskSettings)
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", NullContext)
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda _source_key: NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda _slots: NullContext())
    monkeypatch.setattr(tasks, "_renew_ingestion_leases", lambda *_args: NullContext())
    monkeypatch.setattr(tasks, "run_lifecycle_reconciliation", lambda: None)

    def run(
        source_key: str,
        mode: str,
        dump_path: str | None,
        *,
        entity_key: str | None,
        initialize_graph: bool,
    ) -> dict[str, JsonValue]:
        calls.append((source_key, mode, dump_path, entity_key, initialize_graph))
        return {
            "source_key": source_key,
            "mode": mode,
            "dump_path": dump_path,
            "entity_key": entity_key,
        }

    monkeypatch.setattr(tasks, "run_ingestion", run)

    result = tasks.run_ingestion_task.run(
        "whatsapp_chat", "api", None, entity_key="speedzone"
    )

    assert calls == [("whatsapp_chat", "api", None, "speedzone", False)]
    assert result["entity_key"] == "speedzone"


def test_cli_forwards_optional_entity_key(monkeypatch: MonkeyPatch) -> None:
    calls: list[tuple[str, str, str | None, str | None]] = []
    monkeypatch.setattr(main, "setup_logging", lambda _level: None)
    monkeypatch.setattr(main, "get_settings", lambda: TaskSettings())
    monkeypatch.setattr(
        main,
        "run_ingestion",
        lambda source_key, mode, dump_path, *, entity_key=None: calls.append(
            (source_key, mode, dump_path, entity_key)
        ),
    )

    main.main(
        [
            "--source-key",
            "whatsapp_chat",
            "--mode",
            "api",
            "--entity-key",
            "eko",
        ]
    )

    assert calls == [("whatsapp_chat", "api", None, "eko")]
