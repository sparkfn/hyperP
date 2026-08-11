"""Restricted SQLite reconciliation storage for capability re-gate evidence.

The database intentionally contains only source identifiers and normalized inventory
fields needed to join global stage-history observations to a frozen deal-owner
manifest. Raw Bitrix payloads, URLs, credentials, names, and message content are
never stored here.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.connectors.bitrix_stage_history.spool import (
    _prepare_restricted_directory,
    spool_storage_bytes,
    spool_storage_paths,
)


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


RedactionKey = bytes


def new_redaction_key() -> RedactionKey:
    """Create an in-memory key for one capability evidence run."""
    return os.urandom(32)


def digest_rows(
    rows: Iterable[tuple[object, ...]],
    *,
    domain: str,
    redaction_key: RedactionKey | None,
) -> str:
    """Digest normalized rows, using HMAC when a per-run key is supplied.

    The legacy unkeyed form remains available only for the old compatibility
    probe surface. Re-gate callers must supply one shared key for every pass.
    """
    digest: _Digest
    prefix: str
    if redaction_key is None:
        digest = hashlib.sha256()
        prefix = "sha256:"
    else:
        _validate_redaction_key(redaction_key)
        digest = hmac.new(redaction_key, digestmod=hashlib.sha256)
        _update_value(digest, domain)
        prefix = "hmac-sha256:"
    for row in rows:
        if not isinstance(row, tuple):
            raise RuntimeError("capability reconciliation spool contained an invalid row")
        for value in row:
            _update_value(digest, value)
        digest.update(b"\n")
    return prefix + digest.hexdigest()


def digest_value(
    value: int | str,
    *,
    domain: str,
    redaction_key: RedactionKey | None,
) -> str:
    """Return a domain-separated digest for one redacted scalar."""
    return digest_rows(((value,),), domain=domain, redaction_key=redaction_key)


def _validate_redaction_key(redaction_key: RedactionKey) -> None:
    if not isinstance(redaction_key, bytes) or len(redaction_key) < 32:
        raise ValueError("capability redaction key must contain at least 32 bytes")


def _update_value(digest: _Digest, value: object) -> None:
    if value is None:
        digest.update(b"<null>")
    elif isinstance(value, (str, int)):
        digest.update(str(value).encode("utf-8"))
    else:
        raise RuntimeError("capability reconciliation spool contained an invalid row")
    digest.update(b"\x00")


@dataclass(frozen=True)
class ReconciliationSummary:
    """Redactable local reconciliation accounting for one global stage pass."""

    owner_manifest_digest: str
    global_rows: int
    in_scope_rows: int
    out_of_scope_rows: int
    owners_without_history: int
    global_identity_hash_digest: str
    in_scope_identity_hash_digest: str
    category_inventory_digest: str
    stage_inventory_digest: str
    equal_time_group_digest: str
    current_catalog_stage_count: int | None
    in_scope_historical_stage_count: int | None
    in_scope_historical_stage_missing_catalog_count: int | None
    in_scope_rows_missing_stage_identity: int | None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "owner_manifest_digest": self.owner_manifest_digest,
            "global_rows": self.global_rows,
            "in_scope_rows": self.in_scope_rows,
            "out_of_scope_rows": self.out_of_scope_rows,
            "owners_without_history": self.owners_without_history,
            "global_identity_hash_digest": self.global_identity_hash_digest,
            "in_scope_identity_hash_digest": self.in_scope_identity_hash_digest,
            "category_inventory_digest": self.category_inventory_digest,
            "stage_inventory_digest": self.stage_inventory_digest,
            "equal_time_group_digest": self.equal_time_group_digest,
            "current_catalog_stage_count": self.current_catalog_stage_count,
            "in_scope_historical_stage_count": self.in_scope_historical_stage_count,
            "in_scope_historical_stage_missing_catalog_count": (
                self.in_scope_historical_stage_missing_catalog_count
            ),
            "in_scope_rows_missing_stage_identity": self.in_scope_rows_missing_stage_identity,
        }


class CapabilityReconciliationSpool:
    """A restricted stage spool retaining the fields required for local joining."""

    def __init__(self, directory: Path, pass_number: int) -> None:
        if isinstance(pass_number, bool) or not isinstance(pass_number, int) or pass_number < 1:
            raise ValueError("pass_number must be positive")
        _prepare_restricted_directory(directory)
        self.path = directory / f"stage-reconciliation-pass-{pass_number}.sqlite3"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        os.close(fd)
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.execute(
                "CREATE TABLE events ("
                "stable_id TEXT NOT NULL, canonical_hash TEXT NOT NULL, owner_id TEXT NOT NULL, "
                "category_id TEXT, stage_id TEXT, event_at TEXT NOT NULL, "
                "occurrence_count INTEGER NOT NULL, "
                "PRIMARY KEY (stable_id, canonical_hash)"
                ")"
            )
            self._connection.execute("CREATE INDEX events_owner_id_idx ON events(owner_id)")
            self._connection.commit()
        except BaseException:
            self.close()
            for candidate in spool_storage_paths(self.path):
                candidate.unlink(missing_ok=True)
            raise

    def add(
        self,
        *,
        stable_id: str,
        canonical_hash: str,
        owner_id: str,
        category_id: str | None,
        stage_id: str | None,
        event_at: str,
    ) -> str:
        exact = self._connection.execute(
            "SELECT occurrence_count FROM events WHERE stable_id = ? AND canonical_hash = ?",
            (stable_id, canonical_hash),
        ).fetchone()
        if exact is not None:
            count = exact[0]
            if not isinstance(count, int):
                raise RuntimeError("capability reconciliation spool stored an invalid count")
            self._connection.execute(
                "UPDATE events SET occurrence_count = ? WHERE stable_id = ? AND canonical_hash = ?",
                (count + 1, stable_id, canonical_hash),
            )
            return "same"
        identity = self._connection.execute(
            "SELECT 1 FROM events WHERE stable_id = ? LIMIT 1", (stable_id,)
        ).fetchone()
        self._connection.execute(
            "INSERT INTO events(stable_id, canonical_hash, owner_id, category_id, "
            "stage_id, event_at, occurrence_count) VALUES (?, ?, ?, ?, ?, ?, 1)",
            (stable_id, canonical_hash, owner_id, category_id, stage_id, event_at),
        )
        return "conflict" if identity is not None else "unique"

    def flush(self) -> None:
        self._connection.commit()

    def manifest_digest(self, *, redaction_key: RedactionKey | None = None) -> str:
        return self._digest_rows(
            "SELECT stable_id, canonical_hash, occurrence_count FROM events "
            "ORDER BY stable_id, canonical_hash",
            domain="bitrix-capability-stage-global-identity-hash-v1",
            redaction_key=redaction_key,
        )

    def reconcile(
        self,
        owner_manifest_path: Path,
        owner_manifest_digest: str,
        *,
        redaction_key: RedactionKey | None = None,
        current_catalog_stage_keys: Collection[tuple[str, str]] | None = None,
    ) -> ReconciliationSummary:
        """Join stage rows to the frozen owner manifest without exposing raw identifiers."""
        if not owner_manifest_path.is_file() or owner_manifest_path.is_symlink():
            raise ValueError("restricted owner manifest is unavailable")
        self.flush()
        self._connection.execute("ATTACH DATABASE ? AS owners_db", (str(owner_manifest_path),))
        try:
            actual_owner_manifest_digest = self._digest_rows(
                "SELECT deal_id, category_id, stage_id, occurrence_count FROM owners_db.owners "
                "ORDER BY deal_id",
                domain="bitrix-capability-owner-manifest-v1",
                redaction_key=redaction_key,
            )
            if not hmac.compare_digest(actual_owner_manifest_digest, owner_manifest_digest):
                raise RuntimeError(
                    "capability owner manifest digest did not match restricted storage"
                )
            global_rows = self._single_count(
                "SELECT COALESCE(SUM(occurrence_count), 0) FROM events"
            )
            in_scope_rows = self._single_count(
                "SELECT COALESCE(SUM(e.occurrence_count), 0) FROM events e "
                "JOIN owners_db.owners o ON o.deal_id = e.owner_id"
            )
            owners_without_history = self._single_count(
                "SELECT COUNT(*) FROM owners_db.owners o WHERE NOT EXISTS "
                "(SELECT 1 FROM events e WHERE e.owner_id = o.deal_id)"
            )
            inventory_counts = self._inventory_counts(current_catalog_stage_keys)
            return ReconciliationSummary(
                owner_manifest_digest=actual_owner_manifest_digest,
                global_rows=global_rows,
                in_scope_rows=in_scope_rows,
                out_of_scope_rows=global_rows - in_scope_rows,
                owners_without_history=owners_without_history,
                global_identity_hash_digest=self.manifest_digest(redaction_key=redaction_key),
                in_scope_identity_hash_digest=self._digest_rows(
                    "SELECT e.stable_id, e.canonical_hash, e.occurrence_count FROM events e "
                    "JOIN owners_db.owners o ON o.deal_id = e.owner_id "
                    "ORDER BY e.stable_id, e.canonical_hash",
                    domain="bitrix-capability-stage-in-scope-identity-hash-v1",
                    redaction_key=redaction_key,
                ),
                category_inventory_digest=self._digest_rows(
                    "SELECT COALESCE(category_id, '<null>'), SUM(occurrence_count) FROM events "
                    "GROUP BY category_id ORDER BY category_id",
                    domain="bitrix-capability-stage-category-inventory-v1",
                    redaction_key=redaction_key,
                ),
                stage_inventory_digest=self._digest_rows(
                    "SELECT COALESCE(stage_id, '<null>'), SUM(occurrence_count) FROM events "
                    "GROUP BY stage_id ORDER BY stage_id",
                    domain="bitrix-capability-stage-inventory-v1",
                    redaction_key=redaction_key,
                ),
                equal_time_group_digest=self._digest_rows(
                    "SELECT owner_id, event_at, COUNT(*) FROM events "
                    "GROUP BY owner_id, event_at HAVING COUNT(*) > 1 ORDER BY owner_id, event_at",
                    domain="bitrix-capability-stage-equal-time-groups-v1",
                    redaction_key=redaction_key,
                ),
                current_catalog_stage_count=inventory_counts[0],
                in_scope_historical_stage_count=inventory_counts[1],
                in_scope_historical_stage_missing_catalog_count=inventory_counts[2],
                in_scope_rows_missing_stage_identity=inventory_counts[3],
            )
        finally:
            self._connection.execute("DETACH DATABASE owners_db")

    def _inventory_counts(
        self,
        current_catalog_stage_keys: Collection[tuple[str, str]] | None,
    ) -> tuple[int | None, int | None, int | None, int | None]:
        if current_catalog_stage_keys is None:
            return None, None, None, None
        catalog_keys = set(current_catalog_stage_keys)
        if len(catalog_keys) != len(current_catalog_stage_keys):
            raise ValueError("current catalog stage keys must be distinct")
        if any(not category_id or not stage_id for category_id, stage_id in catalog_keys):
            raise ValueError("current catalog stage keys must be non-empty")
        historical_keys: set[tuple[str, str]] = set()
        rows = self._connection.execute(
            "SELECT DISTINCT e.category_id, e.stage_id FROM events e "
            "JOIN owners_db.owners o ON o.deal_id = e.owner_id "
            "WHERE e.category_id IS NOT NULL AND e.stage_id IS NOT NULL"
        )
        for category_id, stage_id in rows:
            if not isinstance(category_id, str) or not isinstance(stage_id, str):
                raise RuntimeError("capability stage inventory contained an invalid identity")
            historical_keys.add((category_id, stage_id))
        missing_identity_rows = self._single_count(
            "SELECT COALESCE(SUM(e.occurrence_count), 0) FROM events e "
            "JOIN owners_db.owners o ON o.deal_id = e.owner_id "
            "WHERE e.category_id IS NULL OR e.stage_id IS NULL"
        )
        return (
            len(catalog_keys),
            len(historical_keys),
            len(historical_keys - catalog_keys),
            missing_identity_rows,
        )

    def _single_count(self, query: str) -> int:
        row = self._connection.execute(query).fetchone()
        value = row[0] if row is not None else None
        if not isinstance(value, int):
            raise RuntimeError("capability reconciliation spool returned an invalid count")
        return value

    def _digest_rows(
        self,
        query: str,
        *,
        domain: str,
        redaction_key: RedactionKey | None,
    ) -> str:
        rows = (tuple(row) for row in self._connection.execute(query))
        return digest_rows(rows, domain=domain, redaction_key=redaction_key)

    def close(self) -> None:
        connection = getattr(self, "_connection", None)
        if isinstance(connection, sqlite3.Connection):
            connection.close()

    def delete(self) -> None:
        try:
            self.close()
        finally:
            for candidate in spool_storage_paths(self.path):
                candidate.unlink(missing_ok=True)


def reconciliation_spool_storage_bytes(path: Path) -> int:
    return spool_storage_bytes(path)
