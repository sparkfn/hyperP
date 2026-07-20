from __future__ import annotations

from src import main
from src.connectors.base import SourceConnector


class StubConnector(SourceConnector):
    def get_source_key(self) -> str:
        return "bitrix_openlines"

    def fetch_records(self):  # type: ignore[no-untyped-def]
        return iter(())


def test_connector_factory_supports_api_and_backfill(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []
    connector = StubConnector()
    monkeypatch.setattr(
        main,
        "create_bitrix_openlines_connector",
        lambda mode: calls.append(mode) or connector,
    )

    assert main.get_connector("bitrix_openlines", mode="api") is connector
    assert main.get_connector("bitrix_openlines", mode="backfill") is connector
    assert calls == ["api", "backfill"]
