"""Authenticated API connector for the SG Bankruptcy Register."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import httpx

from src.connectors.base import SourceConnector
from src.connectors.sggov.bankruptcy_api_models import (
    BankruptcyExportItem,
    BankruptcyExportPage,
)
from src.connectors.sggov.bankruptcy_common import build_bankruptcy_envelope
from src.models import JsonValue


class SGGovernmentBankruptcyApiConnector(SourceConnector):
    """Yield HyperP envelopes from the scraper's paginated export API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        page_size: int = 500,
        http: httpx.Client | None = None,
        max_attempts: int = 3,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not base_url.strip() or not api_key:
            raise ValueError("SG bankruptcy API URL and key are required")
        if max_attempts < 1:
            raise ValueError("SG bankruptcy API max_attempts must be at least 1")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._page_size = page_size
        self._http = http or httpx.Client(timeout=30.0)
        self._max_attempts = max_attempts
        self._sleeper = sleeper

    def get_source_key(self) -> str:
        return "sgbankruptcy"

    def close(self) -> None:
        self._http.close()

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            params: dict[str, str | int] = {"limit": self._page_size}
            if cursor is not None:
                params["cursor"] = cursor
            response = self._get_page(params)
            page = BankruptcyExportPage.model_validate(response.json())
            for item in page.items:
                yield build_api_envelope(item)
            if page.next_cursor is None:
                return
            if page.next_cursor == cursor or page.next_cursor in seen_cursors:
                raise RuntimeError("SG bankruptcy export cursor did not advance")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

    def _get_page(self, params: dict[str, str | int]) -> httpx.Response:
        for attempt in range(self._max_attempts):
            try:
                response = self._http.get(
                    f"{self._base_url}/api/v1/export/bankruptcy-records",
                    params=params,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
            except httpx.TransportError:
                if attempt + 1 >= self._max_attempts:
                    raise
                self._sleeper(float(2**attempt))
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < self._max_attempts:
                    self._sleeper(float(2**attempt))
                    continue
            response.raise_for_status()
            return response
        raise RuntimeError("SG bankruptcy API retry loop exhausted")


def build_api_envelope(item: BankruptcyExportItem) -> dict[str, JsonValue]:
    """Map one validated export item through the canonical bankruptcy builder."""
    return build_bankruptcy_envelope(
        case_id=str(item.case_id),
        case_number=item.case_number,
        identification_number=item.identification_number,
        person_name=item.person_name,
        latest_document_type=item.latest_document_type,
        latest_document_date=(
            item.latest_document_date.isoformat() if item.latest_document_date else None
        ),
        first_seen_at=item.first_seen_at.isoformat(),
        last_seen_at=item.last_seen_at.isoformat(),
        event_id=str(item.event_id) if item.event_id is not None else None,
        event_type=item.event_type,
        event_date=item.event_date.isoformat() if item.event_date else None,
        trustee_name=item.trustee_name,
        trustee_firm=item.trustee_firm,
        source_document_id=(
            str(item.source_document_id) if item.source_document_id is not None else None
        ),
        source_url=item.source_url,
        document_type=item.document_type,
        document_date=item.document_date.isoformat() if item.document_date else None,
    )
