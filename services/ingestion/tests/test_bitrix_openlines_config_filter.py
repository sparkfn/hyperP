"""Config selection no longer reaches Open Lines dialog lookups.

These scenarios used to drive dialog resolution and message retrieval for the
legacy api/backfill modes. Those modes are retired, so configuration selection
is now moot: a run refuses before any config listing or dialog lookup, whatever
the configuration selects.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import datetime

import pytest
from src.bitrix_ingestion_models import BITRIX_LEGACY_OPENLINES_RETIRED_REASON
from src.connectors.bitrix_openlines.connector import BitrixOpenLinesConnector
from src.connectors.bitrix_openlines.models import (
    ChatReference,
    DialogMetadata,
    OpenLineConfig,
    OpenLineMessage,
)
from src.ingestion_config import BitrixOpenLinesConfig

_LEGACY_REFUSAL = re.escape(BITRIX_LEGACY_OPENLINES_RETIRED_REASON)


class _UntouchableClient:
    """An Open Lines client that fails if a refused mode performs any read."""

    def list_active_configs(self) -> list[OpenLineConfig]:
        raise AssertionError("a refused mode must not list active configs")

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        raise AssertionError("a refused mode must not read CRM chat references")

    def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
        raise AssertionError("a refused mode must not page CRM chat references")

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        raise AssertionError("a refused mode must not read recent chat references")

    def get_dialog(self, chat_id: int) -> DialogMetadata:
        raise AssertionError("a refused mode must not resolve a dialog")

    def get_messages(self, chat_id: int) -> list[OpenLineMessage]:
        raise AssertionError("a refused mode must not read messages")

    def get_history(self, chat_id: int) -> list[OpenLineMessage]:
        raise AssertionError("a refused mode must not read history")

    def close(self) -> None:
        return None


class _StubWatermark:
    """A watermark store that never returns a committed position."""

    def get(self, *, overlap_seconds: int) -> datetime | None:
        return None

    def set(self, value: object) -> None:
        return None

    def close(self) -> None:
        return None


def _connector(config: BitrixOpenLinesConfig, *, mode: str) -> BitrixOpenLinesConnector:
    return BitrixOpenLinesConnector(
        _UntouchableClient(),
        _StubWatermark(),
        config,
        mode=mode,
    )


def _selected_config() -> BitrixOpenLinesConfig:
    """A config whose single configuration ID is included and entity-mapped."""
    return BitrixOpenLinesConfig(
        included_config_ids=["46"],
        entity_by_config_id={"46": "eko"},
    )


def _unselected_config() -> BitrixOpenLinesConfig:
    """A config whose configuration is excluded, so no chat could be selected."""
    return BitrixOpenLinesConfig(
        included_config_ids=["46"],
        excluded_config_ids=["46"],
        entity_by_config_id={"46": "eko"},
    )


@pytest.mark.parametrize("build_config", [_selected_config, _unselected_config])
def test_retired_api_mode_refuses_before_any_dialog_lookup(
    build_config: Callable[[], BitrixOpenLinesConfig],
) -> None:
    """A selected or excluded configuration still refuses before config listing."""
    connector = _connector(build_config(), mode="api")

    with pytest.raises(RuntimeError, match=_LEGACY_REFUSAL):
        list(connector.fetch_records())


def test_retired_backfill_mode_refuses_before_any_dialog_lookup() -> None:
    """Backfill refuses before resolving any dialog, as api mode does."""
    connector = _connector(_selected_config(), mode="backfill")

    with pytest.raises(RuntimeError, match=_LEGACY_REFUSAL):
        list(connector.fetch_records())
