"""Incremental (watermark) connector for SG rental flats via the scraper export API."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from src.connectors.sggov.rental_flats import build_rental_flat_envelope
from src.connectors.sggov.rental_flats_api import (
    _legacy_dump_text,
    _postgres_dump_datetime,
)
from src.connectors.sggov.rental_flats_api_models import (
    RentalFlatCursorPage,
    RentalFlatRow,
)
from src.incremental_connector import IncrementalPage
from src.models import JsonValue


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _build_envelope(item: RentalFlatRow) -> dict[str, JsonValue]:
    block_no = _legacy_dump_text(item.block_no)
    street_name = _legacy_dump_text(item.street_name)
    postal_code = _legacy_dump_text(item.postal_code)
    flat_type = _legacy_dump_text(item.flat_type)
    town_name = _legacy_dump_text(item.town.name)
    town_map_id = _legacy_dump_text(item.town.map_id)
    town_map_zone = (
        _legacy_dump_text(item.town.map_zone)
        if item.town.map_zone is not None
        else None
    )
    last_seen_at = _postgres_dump_datetime(item.last_seen_at)
    raw_payload: dict[str, JsonValue] = {
        "flat": {
            "id": str(item.id),
            "town_id": str(item.town.id),
            "block_no": block_no,
            "street_name": street_name,
            "postal_code": postal_code,
            "flat_type": flat_type,
            "first_seen_at": _postgres_dump_datetime(item.first_seen_at),
            "last_seen_at": last_seen_at,
            "is_active": "t" if item.is_active else "f",
        },
        "town": {
            "id": str(item.town.id),
            "name": town_name,
            "map_id": town_map_id,
            "map_zone": town_map_zone,
        },
    }
    return build_rental_flat_envelope(
        flat_id=str(item.id),
        town_id=str(item.town.id),
        block_no=block_no,
        street_name=street_name,
        postal_code=postal_code,
        flat_type=flat_type,
        town_name=town_name,
        town_map_id=town_map_id,
        town_map_zone=town_map_zone or "",
        is_active=item.is_active,
        observed_at=last_seen_at,
        raw_payload=raw_payload,
    )


class SGGovernmentRentalFlatsIncrementalConnector:
    """Incremental connector for SG rental flats via the scraper export API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        page_size: int = 500,
        timeout_seconds: float = 30.0,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.strip() or not api_key:
            raise ValueError(
                "SG rental flats API URL and key are required"
            )
        if max_attempts < 1:
            raise ValueError(
                "SG rental flats API max_attempts must be at least 1"
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._page_size = page_size
        self._http = http or httpx.Client(timeout=timeout_seconds)
        self._max_attempts = max_attempts
        self._sleeper = sleeper
        self._updated_since: datetime | None = None
        self._cursor: str | None = None
        self._seen_cursors: set[str] = set()
        self._query_opened = False

    def open_query(self, updated_since: datetime | None) -> None:
        self._updated_since = updated_since
        self._cursor = None
        self._seen_cursors = set()
        self._query_opened = True

    def fetch_next_page(self) -> IncrementalPage:
        if not self._query_opened:
            raise RuntimeError(
                "open_query() must be called before fetch_next_page()"
            )
        params: dict[str, str | int] = {"limit": self._page_size}
        if self._cursor is not None:
            params["cursor"] = self._cursor
        if self._updated_since is not None:
            params["updated_since"] = self._updated_since.isoformat()

        response = self._get_page(params)
        page = RentalFlatCursorPage.model_validate(response.json())

        envelopes: list[dict[str, JsonValue]] = [
            _build_envelope(item) for item in page.items
        ]

        if page.items:
            timestamps = [
                _ensure_utc(item.updated_at or item.last_seen_at)
                for item in page.items
            ]
            max_updated_at = max(timestamps)
        else:
            if self._updated_since is not None:
                max_updated_at = _ensure_utc(self._updated_since)
            else:
                max_updated_at = datetime.min.replace(tzinfo=UTC)

        has_more = page.next_cursor is not None

        if page.next_cursor is not None:
            if (
                page.next_cursor == self._cursor
                or page.next_cursor in self._seen_cursors
            ):
                raise RuntimeError(
                    "SG rental flats export cursor did not advance"
                )
            self._seen_cursors.add(page.next_cursor)
            self._cursor = page.next_cursor

        return IncrementalPage(
            records=tuple(envelopes),
            has_more=has_more,
            max_updated_at=max_updated_at,
        )

    def get_source_key(self) -> str:
        return "sgrentalflats"

    def close(self) -> None:
        self._http.close()

    def _get_page(
        self, params: dict[str, str | int],
    ) -> httpx.Response:
        url = (
            f"{self._base_url}/integrations/hyperp/rental-flats"
        )
        for attempt in range(self._max_attempts):
            try:
                response = self._http.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                    },
                )
            except httpx.TransportError:
                if attempt + 1 >= self._max_attempts:
                    raise
                self._sleeper(float(2**attempt))
                continue
            if (
                response.status_code == 429
                or response.status_code >= 500
            ):
                if attempt + 1 < self._max_attempts:
                    self._sleeper(float(2**attempt))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError(
            "SG rental flats API retry loop exhausted"
        )
