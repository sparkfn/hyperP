"""Bitrix generic CRM activity stream with no in-memory deal-owner map."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from src.bitrix_ingestion_models import (
    CrmActivityProjection,
    activity_event_at,
    normalize_history_kind,
)
from src.connectors.base import SourceConnector
from src.connectors.bitrix_openlines.connector import (
    _activity_payload,
    _call_envelope,
    _hash_payload,
)
from src.connectors.bitrix_openlines.models import CrmActivity
from src.models import JsonValue


class CrmActivityClient(Protocol):
    """Read-only generic activity traversal contract."""

    def iter_crm_activities(self) -> Iterator[CrmActivity]: ...

    def close(self) -> None: ...


class BitrixCrmActivityConnector(SourceConnector):
    """Emit activities independently; persistence resolves their deal owner durably."""

    def __init__(self, client: CrmActivityClient) -> None:
        self._client = client

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        emitted_history: set[str] = set()
        emitted_calls: set[str] = set()
        for activity in self._client.iter_crm_activities():
            history_id = f"bitrix-crm-history-{activity.id}"
            if history_id not in emitted_history:
                emitted_history.add(history_id)
                yield _history_envelope(activity)
            if activity.is_call:
                call_id = f"bitrix-call-{activity.id}"
                if call_id not in emitted_calls:
                    emitted_calls.add(call_id)
                    yield _call_envelope(activity, history_id, None)

    def close(self) -> None:
        self._client.close()


def _history_envelope(activity: CrmActivity) -> dict[str, JsonValue]:
    """Create an activity envelope without authorizing it from this scan's deals."""
    raw_payload = _activity_payload(activity)
    projection = CrmActivityProjection(
        history_kind=normalize_history_kind(activity.history_kind),
        event_at=activity_event_at(activity.start_at, activity.observed_at),
    )
    return {
        "source_record_id": f"bitrix-crm-history-{activity.id}",
        "entity_key": None,
        "record_type": "crm_history",
        "ingest_type": "api_incremental",
        "observed_at": (
            activity.observed_at.isoformat() if activity.observed_at is not None else None
        ),
        "record_hash": _hash_payload(raw_payload),
        "raw_payload": raw_payload,
        "history_family": projection.history_family,
        "history_kind": projection.history_kind,
        "history_source": projection.history_source,
        "event_at": projection.event_at_iso,
        "projection_version": projection.projection_version,
        "projection_source": projection.projection_source,
        "parent_ref": {
            "parent_source_system": "bitrix_chat",
            "parent_source_record_id": f"bitrix-crm-deal-{activity.owner_id}",
            "parent_record_type": "crm_deal",
        },
    }
