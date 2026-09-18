"""Bitrix CRM deal stream, independent from generic activity discovery."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from typing import Protocol

from src.bitrix_ingestion_models import BITRIX_LEGACY_OPENLINES_RETIRED_REASON
from src.connectors.base import SourceConnector
from src.connectors.bitrix_openlines.models import CrmDeal, CrmDealCapabilityPage
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue


class CrmDealClient(Protocol):
    """Read-only deal traversal contract used by the deal stream."""

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: Collection[str],
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage: ...

    def get_deals(self, deal_ids: Collection[int]) -> list[CrmDeal]: ...

    @property
    def request_count(self) -> int: ...

    def close(self) -> None: ...


class BitrixCrmDealConnector(SourceConnector):
    """Emit only in-scope CRM deals using the stable Bitrix source identity."""

    def __init__(
        self,
        client: CrmDealClient,
        config: BitrixOpenLinesConfig,
        *,
        upper_deal_id: int,
        last_deal_id: int | None = None,
    ) -> None:
        self._client = client
        self._config = config
        if isinstance(upper_deal_id, bool) or upper_deal_id < 0:
            raise ValueError("upper_deal_id must be non-negative")
        self._upper_deal_id = upper_deal_id
        if last_deal_id is not None and (
            isinstance(last_deal_id, bool) or last_deal_id < 1 or last_deal_id > upper_deal_id
        ):
            raise ValueError("last_deal_id must be within the frozen deal window")
        self._last_deal_id = last_deal_id

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        """Refuse the retired keyset deal path eagerly, before any source I/O."""
        raise RuntimeError(BITRIX_LEGACY_OPENLINES_RETIRED_REASON)

    @property
    def request_count(self) -> int:
        """Expose source HTTP attempts for the enclosing bounded run."""
        return self._client.request_count

    def close(self) -> None:
        self._client.close()
