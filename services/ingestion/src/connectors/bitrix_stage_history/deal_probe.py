"""Bounded, frozen-boundary Bitrix CRM deal census evidence."""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.connectors.bitrix_openlines.models import (
    CrmDealCapabilityItem,
    CrmDealCapabilityPage,
)
from src.connectors.bitrix_stage_history.models import ProbeLimits
from src.connectors.bitrix_stage_history.reconciliation_spool import (
    RedactionKey,
    digest_rows,
    digest_value,
)

_PAGE_SIZE = 50


class DealCapabilityClient(Protocol):
    """Read-only minimal CRM deal capability boundary."""

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: Collection[str],
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage: ...


@dataclass(frozen=True)
class DealPassManifest:
    """Redactable result for one bounded logical-deal-owner census pass."""

    upper_deal_id_digest: str
    calls: int
    pages: int
    raw_rows: int
    unique_owner_rows: int
    duplicate_rows: int
    source_total: int | None
    source_total_consistent: bool
    source_total_matches_rows: bool | None
    owner_manifest_digest: str
    category_inventory_digest: str
    runtime_seconds: float
    spool_bytes: int
    operating_seconds: float = 0.0
    operating_samples: int = 0
    latest_operating_reset_at: float | None = None

    def to_dict(self) -> dict[str, int | float | str | bool | None]:
        return {
            "upper_deal_id_redacted": True,
            "upper_deal_id_digest": self.upper_deal_id_digest,
            "calls": self.calls,
            "pages": self.pages,
            "raw_rows": self.raw_rows,
            "unique_owner_rows": self.unique_owner_rows,
            "duplicate_rows": self.duplicate_rows,
            "source_total": self.source_total,
            "source_total_consistent": self.source_total_consistent,
            "source_total_matches_rows": self.source_total_matches_rows,
            "owner_manifest_digest": self.owner_manifest_digest,
            "category_inventory_digest": self.category_inventory_digest,
            "runtime_seconds": self.runtime_seconds,
            "spool_bytes": self.spool_bytes,
            "operating_seconds": self.operating_seconds,
            "operating_samples": self.operating_samples,
            "latest_operating_reset_at": self.latest_operating_reset_at,
        }


class RestrictedOwnerManifest:
    """Restricted SQLite owner manifest containing no source payloads."""

    def __init__(self, directory: Path, pass_number: int) -> None:
        _prepare_directory(directory)
        self.path = directory / f"deal-owner-pass-{pass_number}.sqlite3"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.close(fd)
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.execute(
                "CREATE TABLE owners ("
                "deal_id TEXT NOT NULL PRIMARY KEY, "
                "category_id TEXT NOT NULL, "
                "stage_id TEXT, "
                "occurrence_count INTEGER NOT NULL"
                ")"
            )
            self._connection.commit()
        except BaseException:
            self.close()
            self.path.unlink(missing_ok=True)
            raise

    def add(self, item: CrmDealCapabilityItem) -> str:
        row = self._connection.execute(
            "SELECT category_id, stage_id, occurrence_count FROM owners WHERE deal_id = ?",
            (item.deal_id,),
        ).fetchone()
        if row is None:
            self._connection.execute(
                "INSERT INTO owners(deal_id, category_id, stage_id, occurrence_count) "
                "VALUES (?, ?, ?, 1)",
                (item.deal_id, item.category_id, item.stage_id),
            )
            return "unique"
        category_id, stage_id, occurrences = row
        if category_id != item.category_id or stage_id != item.stage_id:
            raise RuntimeError("Bitrix deal capability duplicate ID changed within one pass")
        if not isinstance(occurrences, int):
            raise RuntimeError("Bitrix deal capability manifest stored an invalid count")
        self._connection.execute(
            "UPDATE owners SET occurrence_count = ? WHERE deal_id = ?",
            (occurrences + 1, item.deal_id),
        )
        return "duplicate"

    def flush(self) -> None:
        self._connection.commit()

    def manifest_digest(self, *, redaction_key: RedactionKey | None = None) -> str:
        rows = self._connection.execute(
            "SELECT deal_id, category_id, stage_id, occurrence_count FROM owners ORDER BY deal_id"
        )
        return digest_rows(
            (tuple(row) for row in rows),
            domain="bitrix-capability-owner-manifest-v1",
            redaction_key=redaction_key,
        )

    def category_inventory_digest(self, *, redaction_key: RedactionKey | None = None) -> str:
        rows = self._connection.execute(
            "SELECT category_id, count(*) FROM owners GROUP BY category_id ORDER BY category_id"
        )
        return digest_rows(
            (tuple(row) for row in rows),
            domain="bitrix-capability-owner-category-inventory-v1",
            redaction_key=redaction_key,
        )

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if isinstance(connection, sqlite3.Connection):
            connection.close()

    def delete(self) -> None:
        self.close()
        for path in _storage_paths(self.path):
            path.unlink(missing_ok=True)


def freeze_deal_upper_id(client: DealCapabilityClient, category_ids: Collection[str]) -> int:
    """Capture a numeric, redacted-only upper keyset boundary for deal passes."""
    page = client.list_crm_deal_capability_page(
        category_ids=category_ids,
        order_direction="DESC",
    )
    if not page.items:
        return 0
    if len(page.items) > _PAGE_SIZE:
        raise RuntimeError("Bitrix deal boundary probe exceeded the fixed page size")
    page_ids = [_numeric_id(item.deal_id) for item in page.items]
    if page_ids != sorted(page_ids, reverse=True) or len(page_ids) != len(set(page_ids)):
        raise RuntimeError("Bitrix deal boundary probe was not descending")
    return page_ids[0]


def collect_deal_owner_pass(
    client: DealCapabilityClient,
    *,
    category_ids: Collection[str],
    upper_deal_id: int,
    limits: ProbeLimits,
    spool_directory: Path,
    pass_number: int,
    redaction_key: RedactionKey | None = None,
) -> tuple[DealPassManifest, RestrictedOwnerManifest]:
    """Collect one strict keyset census below a single frozen deal boundary."""
    if isinstance(upper_deal_id, bool) or upper_deal_id < 0:
        raise ValueError("upper_deal_id must be non-negative")
    if not tuple(category_ids):
        raise ValueError("deal owner census requires at least one included category")
    spool = RestrictedOwnerManifest(spool_directory, pass_number)
    started = time.monotonic()
    calls = pages = raw_rows = unique_rows = duplicate_rows = 0
    source_total: int | None = None
    source_total_observed = False
    source_total_consistent = True
    operating_seconds = 0.0
    operating_samples = 0
    latest_operating_reset_at: float | None = None
    cursor: int | None = None
    try:
        while upper_deal_id > 0:
            _check_limits(limits, started, calls, raw_rows, spool.path)
            if calls >= limits.max_calls:
                raise RuntimeError("Bitrix deal capability call limit exceeded")
            page = client.list_crm_deal_capability_page(
                category_ids=category_ids,
                greater_than_id=cursor,
                less_than_or_equal_to_id=upper_deal_id,
                order_direction="ASC",
            )
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
            if not source_total_observed:
                source_total = page.total
                source_total_observed = True
            elif page.total != source_total:
                source_total_consistent = False
            page_ids = [_numeric_id(item.deal_id) for item in page.items]
            if page_ids != sorted(page_ids) or len(page_ids) != len(set(page_ids)):
                raise RuntimeError("Bitrix deal keyset page was not strictly increasing")
            if page_ids and cursor is not None and page_ids[0] <= cursor:
                raise RuntimeError("Bitrix deal keyset did not advance")
            if any(value > upper_deal_id for value in page_ids):
                raise RuntimeError("Bitrix deal keyset exceeded its frozen upper boundary")
            for item in page.items:
                raw_rows += 1
                _check_limits(limits, started, calls, raw_rows, spool.path)
                if spool.add(item) == "unique":
                    unique_rows += 1
                else:
                    duplicate_rows += 1
            if len(page.items) < _PAGE_SIZE:
                break
            if not page_ids:
                raise RuntimeError("Bitrix deal keyset returned an invalid full page")
            cursor = page_ids[-1]
        spool.flush()
        _check_limits(limits, started, calls, raw_rows, spool.path)
        total_matches = (
            source_total == raw_rows
            if source_total_consistent and source_total is not None
            else None
        )
        boundary_digest = digest_value(
            upper_deal_id,
            domain="bitrix-capability-deal-upper-id-v1",
            redaction_key=redaction_key,
        )
        return (
            DealPassManifest(
                upper_deal_id_digest=boundary_digest,
                calls=calls,
                pages=pages,
                raw_rows=raw_rows,
                unique_owner_rows=unique_rows,
                duplicate_rows=duplicate_rows,
                source_total=source_total,
                source_total_consistent=source_total_consistent,
                source_total_matches_rows=total_matches,
                owner_manifest_digest=spool.manifest_digest(redaction_key=redaction_key),
                category_inventory_digest=spool.category_inventory_digest(
                    redaction_key=redaction_key
                ),
                runtime_seconds=time.monotonic() - started,
                spool_bytes=_storage_bytes(spool.path),
                operating_seconds=operating_seconds,
                operating_samples=operating_samples,
                latest_operating_reset_at=latest_operating_reset_at,
            ),
            spool,
        )
    except BaseException:
        spool.delete()
        raise


def deal_manifests_are_identical(first: DealPassManifest, second: DealPassManifest) -> bool:
    """Compare source-set evidence while excluding mutable measurements."""
    return (
        first.upper_deal_id_digest == second.upper_deal_id_digest
        and first.calls == second.calls
        and first.pages == second.pages
        and first.raw_rows == second.raw_rows
        and first.unique_owner_rows == second.unique_owner_rows
        and first.duplicate_rows == second.duplicate_rows
        and first.source_total == second.source_total
        and first.source_total_consistent
        and second.source_total_consistent
        and first.source_total_matches_rows == second.source_total_matches_rows
        and first.source_total_matches_rows is not False
        and first.owner_manifest_digest == second.owner_manifest_digest
        and first.category_inventory_digest == second.category_inventory_digest
    )


def _numeric_id(value: str) -> int:
    if not value.isdigit():
        raise RuntimeError("Bitrix deal capability requires numeric IDs")
    return int(value)


def _check_limits(limits: ProbeLimits, started: float, calls: int, rows: int, path: Path) -> None:
    if calls > limits.max_calls:
        raise RuntimeError("Bitrix deal capability call limit exceeded")
    if rows > limits.max_rows:
        raise RuntimeError("Bitrix deal capability row limit exceeded")
    if time.monotonic() - started > limits.max_runtime_seconds:
        raise RuntimeError("Bitrix deal capability runtime limit exceeded")
    if _storage_bytes(path) > limits.max_spool_bytes:
        raise RuntimeError("Bitrix deal capability spool limit exceeded")


def _prepare_directory(directory: Path) -> None:
    try:
        path_stat = directory.lstat()
    except FileNotFoundError:
        directory.mkdir(parents=True, mode=0o700)
        path_stat = directory.lstat()
    if stat.S_ISLNK(path_stat.st_mode):
        raise ValueError("restricted owner manifest directory cannot be a symlink")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise ValueError("restricted owner manifest directory must be a directory")
    if stat.S_IMODE(path_stat.st_mode) & 0o077:
        raise ValueError("restricted owner manifest directory permissions are too broad")


def _storage_paths(path: Path) -> tuple[Path, ...]:
    return (path, Path(f"{path}-journal"), Path(f"{path}-wal"), Path(f"{path}-shm"))


def _storage_bytes(path: Path) -> int:
    return sum(candidate.stat().st_size for candidate in _storage_paths(path) if candidate.exists())
