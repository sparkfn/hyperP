"""Bitrix CRM deal stream, independent from generic activity discovery."""

from __future__ import annotations

from collections.abc import Collection, Iterator
from typing import Protocol

from src.connectors.base import SourceConnector
from src.connectors.bitrix_openlines.connector import (
    _CrmEntityMappingError,
    _deal_envelope,
    _validate_included_crm_category_mappings,
)
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
        category_ids = _validate_included_crm_category_mappings(self._config)
        cursor = self._last_deal_id
        if cursor == self._upper_deal_id:
            return
        while self._upper_deal_id > 0:
            page = self._client.list_crm_deal_capability_page(
                category_ids=category_ids,
                greater_than_id=cursor,
                less_than_or_equal_to_id=self._upper_deal_id,
            )
            ids = [int(item.deal_id) for item in page.items]
            if ids != sorted(ids) or len(ids) != len(set(ids)):
                raise RuntimeError("Bitrix deal backfill keyset was not strictly increasing")
            if cursor is not None and ids and ids[0] <= cursor:
                raise RuntimeError("Bitrix deal backfill keyset did not advance")
            deals = self._client.get_deals(ids)
            if [int(deal.id) for deal in deals] != ids:
                raise RuntimeError("Bitrix deal hydration did not preserve capability order")
            for item, deal in zip(page.items, deals, strict=True):
                category_id = deal.category_id
                if category_id != item.category_id:
                    raise RuntimeError("Bitrix deal changed category during bounded hydration")
                entity_key = self._config.entity_by_crm_category_id.get(category_id)
                if entity_key is None:
                    raise _CrmEntityMappingError(
                        f"Bitrix CRM deal {deal.id} category {category_id!r} has no entity mapping"
                    )
                yield _deal_envelope(
                    deal,
                    entity_key,
                    source_instance_id=self._config.source_instance_id,
                )
            if len(page.items) < 50:
                return
            if not ids:
                raise RuntimeError("Bitrix deal backfill returned an invalid full page")
            cursor = ids[-1]

    @property
    def request_count(self) -> int:
        """Expose source HTTP attempts for the enclosing bounded run."""
        return self._client.request_count

    def close(self) -> None:
        self._client.close()
