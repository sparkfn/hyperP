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
from src.connectors.bitrix_openlines.models import CrmActivity, CrmActivityCapabilityPage
from src.models import JsonValue


class CrmActivityClient(Protocol):
    """Read-only generic activity traversal contract."""

    def list_crm_activity_capability_page(
        self,
        *,
        greater_than_id: int | None,
        less_than_or_equal_to_id: int,
        order_direction: str = "ASC",
    ) -> CrmActivityCapabilityPage: ...

    def close(self) -> None: ...


class BitrixCrmActivityConnector(SourceConnector):
    """Emit activities independently; persistence resolves their deal owner durably."""

    def __init__(
        self,
        client: CrmActivityClient,
        *,
        upper_activity_id: int,
        last_activity_id: int | None = None,
    ) -> None:
        self._client = client
        if isinstance(upper_activity_id, bool) or upper_activity_id < 0:
            raise ValueError("upper_activity_id must be non-negative")
        self._upper_activity_id = upper_activity_id
        if last_activity_id is not None and (
            isinstance(last_activity_id, bool)
            or last_activity_id < 1
            or last_activity_id > upper_activity_id
        ):
            raise ValueError("last_activity_id must be within the frozen activity window")
        self._last_activity_id = last_activity_id

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        cursor = self._last_activity_id
        while self._upper_activity_id > 0:
            page = self._client.list_crm_activity_capability_page(
                greater_than_id=cursor,
                less_than_or_equal_to_id=self._upper_activity_id,
            )
            ids = [int(activity.id) for activity in page.items]
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise RuntimeError("Bitrix activity keyset was not strictly increasing")
            if cursor is not None and ids and ids[0] <= cursor:
                raise RuntimeError("Bitrix activity keyset did not advance")
            for activity in page.items:
                history_id = f"bitrix-crm-history-{activity.id}"
                yield _history_envelope(activity)
                if activity.is_call:
                    yield _call_envelope(activity, history_id, None)
            if len(page.items) < 50:
                return
            if not ids:
                raise RuntimeError("Bitrix activity keyset returned an invalid full page")
            cursor = ids[-1]

    def close(self) -> None:
        self._client.close()


def _history_envelope(activity: CrmActivity) -> dict[str, JsonValue]:
    """Create an activity envelope without authorizing it from this scan's deals."""
    raw_payload = _activity_payload(activity)
    # A call activity is one bounded source unit represented by two graph
    # records. The history write must not advance the activity cursor before
    # its companion call commits.
    raw_payload["has_call_record"] = activity.is_call
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
