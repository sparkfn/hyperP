"""Retirement of the legacy Bitrix Open Lines api/backfill connector modes.

The legacy api and backfill ingestion paths were replaced by the bounded
adapter, so both modes now refuse before any source access. Only the shared
mode-scoped guard is covered here; the separate retirement of activity-based
discovery is covered by ``test_paged_activity_discovery_is_retired``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
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


def _connector(*, mode: str, config: BitrixOpenLinesConfig) -> BitrixOpenLinesConnector:
    return BitrixOpenLinesConnector(
        _UntouchableClient(),
        _StubWatermark(),
        config,
        mode=mode,
    )


def _unselectable_config() -> BitrixOpenLinesConfig:
    """A config no configuration ID can select, so no record could be emitted."""
    return BitrixOpenLinesConfig(included_channel_types=[], entity_by_config_id={})


@pytest.mark.parametrize("mode", ["api", "backfill"])
def test_legacy_modes_refuse_before_any_source_access(mode: str) -> None:
    """Retired api/backfill ingestion refuses instead of emitting legacy records."""
    connector = _connector(mode=mode, config=_unselectable_config())

    with pytest.raises(RuntimeError, match=_LEGACY_REFUSAL):
        list(connector.fetch_records())


def test_dump_mode_is_not_refused_by_the_legacy_guard() -> None:
    """Dump mode clears the legacy guard, so the retirement is mode-scoped."""
    connector = _connector(mode="dump", config=_unselectable_config())

    assert list(connector.fetch_records()) == []
