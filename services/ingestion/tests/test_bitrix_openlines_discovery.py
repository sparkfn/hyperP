"""Retirement of the hybrid CRM-activity Open Lines discovery.

``stream_chats`` and ``discover_chats`` used to union CRM-activity and
recent-dialog discovery, de-duplicating chat IDs and retaining typed provenance.
Activity discovery is permanently retired, so both refuse before reading the
client at all.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from src.bitrix_ingestion_models import CRM_ACTIVITY_SOURCE_ACCESS_RETIRED_REASON
from src.connectors.bitrix_openlines.discovery import discover_chats, stream_chats
from src.connectors.bitrix_openlines.models import ChatReference

_REFUSAL = re.escape(CRM_ACTIVITY_SOURCE_ACCESS_RETIRED_REASON)


class _UntouchableDiscoveryClient:
    """A discovery client that fails if a retired path reads anything."""

    def iter_crm_chat_refs(self) -> list[ChatReference]:
        raise AssertionError("retired discovery must not read CRM chat references")

    def iter_crm_chat_ref_pages(self) -> Iterator[list[ChatReference]]:
        raise AssertionError("retired discovery must not page CRM chat references")

    def iter_recent_chat_refs(self, page_size: int) -> list[ChatReference]:
        raise AssertionError("retired discovery must not read recent chat references")


def test_hybrid_discovery_is_refused_before_any_client_read() -> None:
    """``discover_chats`` refuses instead of unioning CRM and recent discovery."""
    with pytest.raises(RuntimeError, match=_REFUSAL):
        discover_chats(_UntouchableDiscoveryClient(), recent_page_size=25)


def test_streaming_discovery_is_refused_before_any_client_read() -> None:
    """``stream_chats`` refuses instead of yielding hybrid discovery pages."""
    with pytest.raises(RuntimeError, match=_REFUSAL):
        stream_chats(_UntouchableDiscoveryClient(), recent_page_size=25)


def test_chat_reference_has_typed_crm_provenance_fields() -> None:
    """The provenance fields stay on the model even though discovery is retired."""
    assert {
        "activity_ids",
        "crm_owner_references",
        "provider_references",
    }.issubset(ChatReference.__dataclass_fields__)
