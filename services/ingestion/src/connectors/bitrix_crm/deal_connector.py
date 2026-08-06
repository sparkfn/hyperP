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
from src.connectors.bitrix_openlines.crm_deal_filter import CrmDealPage
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue


class CrmDealClient(Protocol):
    """Read-only deal traversal contract used by the deal stream."""

    def iter_crm_deal_pages(self, category_ids: Collection[str]) -> Iterator[CrmDealPage]: ...

    def close(self) -> None: ...


class BitrixCrmDealConnector(SourceConnector):
    """Emit only in-scope CRM deals using the stable Bitrix source identity."""

    def __init__(self, client: CrmDealClient, config: BitrixOpenLinesConfig) -> None:
        self._client = client
        self._config = config

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        category_ids = _validate_included_crm_category_mappings(self._config)
        included = frozenset(category_ids)
        for page in self._client.iter_crm_deal_pages(category_ids):
            for deal in page.deals:
                category_id = deal.category_id
                if category_id is None or category_id not in included:
                    continue
                entity_key = self._config.entity_by_crm_category_id.get(category_id)
                if entity_key is None:
                    raise _CrmEntityMappingError(
                        f"Bitrix CRM deal {deal.id} category {category_id!r} has no entity mapping"
                    )
                yield _deal_envelope(deal, entity_key)

    def close(self) -> None:
        self._client.close()
