"""Incremental CRM deal connector using Bitrix DATE_MODIFY watermark."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime

from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_openlines.connector import (
    _CrmEntityMappingError,
    _validate_included_crm_category_mappings,
    build_crm_deal_envelope,
)
from src.connectors.bitrix_openlines.crm_deal_filter import CrmDealPage
from src.incremental_connector import IncrementalPage
from src.ingestion_config import BitrixOpenLinesConfig
from src.models import JsonValue

logger = logging.getLogger(__name__)


class BitrixCrmDealIncrementalConnector:
    """Yield CRM deal pages through the IncrementalConnector protocol."""

    def __init__(
        self,
        client: BitrixOpenLinesClient,
        config: BitrixOpenLinesConfig,
    ) -> None:
        self._client = client
        self._config = config
        self._iterator: Iterator[CrmDealPage] | None = None
        self._lookahead: CrmDealPage | None = None
        self._updated_since: datetime | None = None
        self._max_updated_at: datetime | None = None

    def get_source_key(self) -> str:
        return "bitrix_chat"

    def open_query(self, updated_since: datetime | None) -> None:
        self._updated_since = updated_since
        category_ids = _validate_included_crm_category_mappings(self._config)
        self._iterator = self._client.iter_crm_deal_pages(
            category_ids,
            modified_since=updated_since,
        )
        self._lookahead = None
        self._max_updated_at = None
        self._advance()

    def fetch_next_page(self) -> IncrementalPage:
        if self._iterator is None:
            raise RuntimeError("open_query has not been called")
        current = self._lookahead
        if current is None:
            fallback = self._updated_since or datetime.now(UTC)
            return IncrementalPage(
                records=(),
                has_more=False,
                max_updated_at=fallback,
            )
        envelopes = self._build_envelopes(current)
        self._advance()
        has_more = self._lookahead is not None
        max_ts = self._max_updated_at or self._updated_since or datetime.now(UTC)
        return IncrementalPage(
            records=tuple(envelopes),
            has_more=has_more,
            max_updated_at=max_ts,
        )

    def close(self) -> None:
        self._iterator = None
        self._lookahead = None
        self._client.close()

    def _advance(self) -> None:
        assert self._iterator is not None
        try:
            self._lookahead = next(self._iterator)
        except StopIteration:
            self._lookahead = None

    def _build_envelopes(self, page: CrmDealPage) -> list[dict[str, JsonValue]]:
        category_entities = self._config.entity_by_crm_category_id
        envelopes: list[dict[str, JsonValue]] = []
        for deal in page.deals:
            entity_key = category_entities.get(deal.category_id or "")
            if entity_key is None:
                raise _CrmEntityMappingError(
                    f"Bitrix CRM deal {deal.id} category {deal.category_id!r} has no entity mapping"
                )
            envelopes.append(
                build_crm_deal_envelope(
                    deal,
                    entity_key,
                    source_instance_id=self._config.source_instance_id,
                )
            )
            if deal.observed_at is not None:
                if self._max_updated_at is None or deal.observed_at > self._max_updated_at:
                    self._max_updated_at = deal.observed_at
        return envelopes
