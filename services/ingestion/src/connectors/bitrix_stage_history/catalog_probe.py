"""Bounded read-only current Bitrix deal-stage catalog evidence."""

from __future__ import annotations

import time
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from src.connectors.bitrix_openlines.models import CrmDealStageCatalogItem, CrmDealStageCatalogPage
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.reconciliation_spool import RedactionKey, digest_rows


class DealStageCatalogClient(Protocol):
    """The capability-only current deal-stage catalog boundary."""

    def list_crm_deal_stage_catalog_page(
        self, *, category_id: int, start: int = 0
    ) -> CrmDealStageCatalogPage: ...


@dataclass(frozen=True)
class CatalogManifest:
    """Redacted evidence describing the selected current stage catalog."""

    calls: int
    pages: int
    raw_rows: int
    unique_rows: int
    duplicate_rows: int
    conflict_rows: int
    source_total_consistent: bool
    source_total_matches_rows: bool | None
    catalog_digest: str
    runtime_seconds: float
    operating_seconds: float
    operating_samples: int
    latest_operating_reset_at: float | None

    def to_dict(self) -> dict[str, int | float | str | bool | None]:
        return {
            "calls": self.calls,
            "pages": self.pages,
            "raw_rows": self.raw_rows,
            "unique_rows": self.unique_rows,
            "duplicate_rows": self.duplicate_rows,
            "conflict_rows": self.conflict_rows,
            "source_total_consistent": self.source_total_consistent,
            "source_total_matches_rows": self.source_total_matches_rows,
            "catalog_digest": self.catalog_digest,
            "runtime_seconds": self.runtime_seconds,
            "operating_seconds": self.operating_seconds,
            "operating_samples": self.operating_samples,
            "latest_operating_reset_at": self.latest_operating_reset_at,
        }


def collect_current_stage_catalog(
    client: DealStageCatalogClient,
    *,
    category_ids: Collection[str],
    limits: ProbeLimits,
    redaction_key: RedactionKey,
) -> tuple[CatalogManifest, tuple[tuple[str, str], ...]]:
    """Read all selected catalog pages with the same explicit resource ceilings."""
    categories = tuple(sorted({int(value) for value in category_ids}))
    if not categories:
        raise ValueError("stage catalog requires at least one category")
    started = time.monotonic()
    calls = pages = raw_rows = unique_rows = duplicate_rows = conflict_rows = 0
    source_total_consistent = True
    total_matches_rows: bool | None = True
    operating_seconds = 0.0
    operating_samples = 0
    latest_operating_reset_at: float | None = None
    records: dict[tuple[str, str], str | None] = {}
    for category_id in categories:
        start = 0
        declared_total: int | None = None
        total_observed = False
        category_raw_rows = 0
        while True:
            _check_limits(limits, started, calls, raw_rows)
            if calls >= limits.max_calls:
                raise RuntimeError("Bitrix stage catalog call limit exceeded")
            page = client.list_crm_deal_stage_catalog_page(category_id=category_id, start=start)
            calls += 1
            pages += 1
            if page.operating is not None:
                operating_seconds += page.operating
                operating_samples += 1
            if page.operating_reset_at is not None:
                latest_operating_reset_at = max(
                    page.operating_reset_at,
                    latest_operating_reset_at
                    if latest_operating_reset_at is not None
                    else page.operating_reset_at,
                )
            if not total_observed:
                declared_total = page.total
                total_observed = True
            elif page.total != declared_total:
                source_total_consistent = False
            for item in page.items:
                raw_rows += 1
                category_raw_rows += 1
                _check_limits(limits, started, calls, raw_rows)
                disposition = _add_record(records, item)
                if disposition == "unique":
                    unique_rows += 1
                elif disposition == "duplicate":
                    duplicate_rows += 1
                else:
                    conflict_rows += 1
            if page.next_start is None:
                if declared_total is None:
                    if total_matches_rows is True:
                        total_matches_rows = None
                elif declared_total != category_raw_rows:
                    total_matches_rows = False
                break
            start = page.next_start
    rows = tuple(
        (category_id, stage_id, records[(category_id, stage_id)])
        for category_id, stage_id in sorted(records)
    )
    return (
        CatalogManifest(
            calls=calls,
            pages=pages,
            raw_rows=raw_rows,
            unique_rows=unique_rows,
            duplicate_rows=duplicate_rows,
            conflict_rows=conflict_rows,
            source_total_consistent=source_total_consistent,
            source_total_matches_rows=total_matches_rows,
            catalog_digest=digest_rows(
                rows,
                domain="bitrix-capability-current-stage-catalog-v1",
                redaction_key=redaction_key,
            ),
            runtime_seconds=time.monotonic() - started,
            operating_seconds=operating_seconds,
            operating_samples=operating_samples,
            latest_operating_reset_at=latest_operating_reset_at,
        ),
        tuple(sorted(records)),
    )


def _add_record(records: dict[tuple[str, str], str | None], item: CrmDealStageCatalogItem) -> str:
    key = (item.category_id, item.stage_id)
    existing = records.get(key)
    if key not in records:
        records[key] = item.semantic_id
        return "unique"
    if existing == item.semantic_id:
        return "duplicate"
    return "conflict"


def _check_limits(limits: ProbeLimits, started: float, calls: int, rows: int) -> None:
    if calls > limits.max_calls:
        raise RuntimeError("Bitrix stage catalog call limit exceeded")
    if rows > limits.max_rows:
        raise RuntimeError("Bitrix stage catalog row limit exceeded")
    if time.monotonic() - started > limits.max_runtime_seconds:
        raise RuntimeError("Bitrix stage catalog runtime limit exceeded")
