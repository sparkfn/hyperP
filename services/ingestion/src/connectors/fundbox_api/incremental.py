"""Incremental (watermark) connector for Fundbox API resources."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from src.connectors.fundbox_api.client import (
    FundboxApiClient,
    create_fundbox_api_client,
)
from src.connectors.fundbox_api.connectors import (
    FundboxContactsApiConnector,
    FundboxSalesApiConnector,
    FundboxUsersApiConnector,
)
from src.connectors.fundbox_api.models import PageMeta
from src.incremental_connector import IncrementalPage
from src.models import JsonValue


class FundboxIncrementalConnector:
    def __init__(
        self,
        client: FundboxApiClient,
        resource: str,
        source_key: str,
        build_record: Callable[[dict[str, JsonValue]], dict[str, JsonValue]],
    ) -> None:
        self._client = client
        self._resource = resource
        self._source_key = source_key
        self._build_record = build_record
        self._updated_since: datetime | None = None
        self._updated_since_iso: str | None = None
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._exhausted = False

    def open_query(self, updated_since: datetime | None) -> None:
        self._updated_since = updated_since
        self._updated_since_iso = updated_since.isoformat() if updated_since is not None else None
        self._cursor = None
        self._seen_cursors = set()
        self._exhausted = False

    def fetch_next_page(self) -> IncrementalPage:
        if self._exhausted:
            raise RuntimeError("Connector is exhausted; call open_query to start a new query")
        records, meta = self._client.fetch_page(
            self._resource,
            cursor=self._cursor,
            updated_since=self._updated_since_iso if self._cursor is None else None,
        )
        envelopes = tuple(self._build_record(composite) for composite in records)
        max_updated_at = self._compute_max_updated_at(records, meta)
        if meta.has_more:
            assert meta.next_cursor is not None
            if meta.next_cursor in self._seen_cursors:
                raise ValueError("Fundbox API returned a repeated cursor")
            self._seen_cursors.add(meta.next_cursor)
            self._cursor = meta.next_cursor
        else:
            self._exhausted = True
        return IncrementalPage(
            records=envelopes,
            has_more=meta.has_more,
            max_updated_at=max_updated_at,
        )

    def get_source_key(self) -> str:
        return self._source_key

    def close(self) -> None:
        self._client.close()

    def _compute_max_updated_at(
        self,
        records: list[dict[str, JsonValue]],
        meta: PageMeta,
    ) -> datetime:
        page_max: datetime | None = None
        for record in records:
            effective = record.get("effective_updated_at")
            if isinstance(effective, str):
                parsed = datetime.fromisoformat(effective)
                if page_max is None or parsed > page_max:
                    page_max = parsed
        if page_max is not None:
            return page_max
        if self._updated_since is not None:
            return self._updated_since
        return datetime.now(UTC)


def create_fundbox_users_incremental() -> FundboxIncrementalConnector:
    return FundboxIncrementalConnector(
        create_fundbox_api_client(),
        "users",
        "fundbox",
        FundboxUsersApiConnector.build_record,
    )


def create_fundbox_contacts_incremental() -> FundboxIncrementalConnector:
    return FundboxIncrementalConnector(
        create_fundbox_api_client(),
        "contacts",
        "fundbox:contacts",
        FundboxContactsApiConnector.build_record,
    )


def create_fundbox_sales_incremental() -> FundboxIncrementalConnector:
    return FundboxIncrementalConnector(
        create_fundbox_api_client(),
        "sales",
        "fundbox:sales",
        FundboxSalesApiConnector.build_record,
    )
