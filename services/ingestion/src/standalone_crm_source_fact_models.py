"""Strict page contracts for standalone CRM contact and lead source facts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from src.connectors.bitrix_openlines.models import CrmContact
from src.models import SourceRecordEnvelope
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import ContactSourceChildEnvelope, LeadSourceChildEnvelope
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmUnitAccountingDelta,
)

type SourceFactEnvelope = ContactSourceChildEnvelope | LeadSourceChildEnvelope

MAX_STANDALONE_CRM_SOURCE_FACT_PAGE_ROWS = 50
type SourceFactCommitDecision = Literal[
    "committed",
    "replayed",
    "conflict",
    "authority_rejected",
    "attempt_exhausted",
    "occurrence_exhausted",
]


@dataclass(frozen=True)
class StandaloneCrmSourceFactPage:
    """One already-fetched, succeeded-call contact or lead page."""

    envelope: SourceFactEnvelope
    call_intent_id: str
    cursor: int
    expected_checkpoint: StandaloneCrmCheckpoint
    rows: tuple[CrmContact, ...]
    defer_contact_cursor: bool = False

    def __post_init__(self) -> None:
        if not self.call_intent_id.strip():
            raise ValueError("call_intent_id must be non-empty")
        if self.cursor < 0 or self.cursor != self.envelope.last_committed_id:
            raise ValueError("page cursor must equal the parent-issued cursor")
        expected = self.expected_checkpoint
        if (
            expected.census_id != self.envelope.unit.census_id
            or expected.stream_kind != self.envelope.unit.stream_kind
            or expected.frozen_upper_id != self.envelope.frozen_upper_id
            or expected.last_committed_id != self.cursor
            or expected.revision_id is not None
            or expected.generation != self.envelope.unit.generation
            or expected.fence_token != self.envelope.unit.fence_token
        ):
            raise ValueError("expected checkpoint must match source-child authority exactly")
        if expected.binding_subject_id is not None or expected.binding_offset is not None:
            raise ValueError("source-fact pages cannot carry contact binding position")
        if not self.rows:
            raise ValueError("source-fact page must contain at least one row")
        if len(self.rows) > MAX_STANDALONE_CRM_SOURCE_FACT_PAGE_ROWS:
            raise ValueError("source-fact page cannot exceed the fixed 50-row Bitrix page limit")
        previous = self.cursor
        for row in self.rows:
            row_id = _strict_row_id(row.id)
            if row.kind != self.envelope.unit.stream_kind:
                raise ValueError("row kind must match page stream authority")
            if row_id <= previous:
                raise ValueError("page row IDs must be strictly increasing after cursor")
            if row_id > self.envelope.frozen_upper_id:
                raise ValueError("page row ID exceeds frozen upper bound")
            previous = row_id
        if self.defer_contact_cursor and (
            self.envelope.unit.stream_kind != "contact" or len(self.rows) != 1
        ):
            raise ValueError("deferred source-fact pages require exactly one contact")

    @property
    def proposed_cursor(self) -> int:
        return _strict_row_id(self.rows[-1].id)

    @property
    def content_digest(self) -> str:
        return _digest({"cursor": self.cursor, "rows": [_row_payload(row) for row in self.rows]})


@dataclass(frozen=True)
class MappedSourceFactRow:
    row_id: int
    envelope: SourceRecordEnvelope


@dataclass(frozen=True)
class MalformedSourceFactRow:
    row_id: int
    reason: str


@dataclass(frozen=True)
class StandaloneCrmSourceFactMutation:
    """Mapped page content; duplicate planning remains transaction-local."""

    page: StandaloneCrmSourceFactPage
    mapped_rows: tuple[MappedSourceFactRow, ...]
    malformed_rows: tuple[MalformedSourceFactRow, ...]

    def __post_init__(self) -> None:
        all_ids = [item.row_id for item in self.mapped_rows] + [
            item.row_id for item in self.malformed_rows
        ]
        expected_ids = [_strict_row_id(row.id) for row in self.page.rows]
        if sorted(all_ids) != expected_ids or len(all_ids) != len(set(all_ids)):
            raise ValueError("mapped and malformed rows must cover the page exactly once")

    @property
    def processed_rows(self) -> int:
        return len(self.page.rows)

    @property
    def failed_rows(self) -> int:
        return len(self.malformed_rows)


@dataclass(frozen=True)
class StandaloneCrmSourceFactReceipt:
    """Exact persisted source-fact identity reused by the #303 handoff."""

    row_id: int
    source_record_pk: str
    source_record_version: int
    record_hash: str
    observed_at: str
    lead_company_id: str | None = None

    def __post_init__(self) -> None:
        _strict_row_id(str(self.row_id))
        if not self.source_record_pk or self.source_record_version < 1:
            raise ValueError("source-fact receipt must identify one persisted source version")
        if not self.record_hash or not self.observed_at:
            raise ValueError("source-fact receipt must retain hash and observed_at")
        if self.lead_company_id is not None:
            _strict_row_id(self.lead_company_id)


@dataclass(frozen=True)
class StandaloneCrmSourceFactCommitResult:
    decision: SourceFactCommitDecision
    processed_rows: int = 0
    skipped_rows: int = 0
    failed_rows: int = 0
    receipts: tuple[StandaloneCrmSourceFactReceipt, ...] = ()

    @property
    def committed(self) -> bool:
        return self.decision == "committed"


def build_source_fact_commit(
    mutation: StandaloneCrmSourceFactMutation,
    *,
    skipped_rows: int,
) -> StandaloneCrmAtomicUnitCommit[StandaloneCrmSourceFactMutation]:
    """Build the exact #301 atomic unit commit after deterministic duplicate planning.

    Mapping knows malformed rows; source-version duplicate status is deliberately read
    under the same locked Neo4j transaction.  The repository therefore rebuilds this
    immutable typed request after it plans every mapped row and before any persist.
    """
    if skipped_rows < 0 or skipped_rows > len(mutation.mapped_rows):
        raise ValueError("skipped_rows must be within mapped rows")
    page = mutation.page
    expected = page.expected_checkpoint
    delta = StandaloneCrmUnitAccountingDelta(
        mutation.processed_rows,
        skipped_rows,
        mutation.failed_rows,
    )
    deferred_contact = page.defer_contact_cursor
    proposed = StandaloneCrmCheckpoint(
        expected.census_id,
        expected.stream_kind,
        expected.frozen_upper_id,
        None,
        expected.last_committed_id if deferred_contact else page.proposed_cursor,
        page.proposed_cursor if deferred_contact else None,
        0 if deferred_contact else None,
        expected.processed_rows + delta.processed_rows,
        expected.skipped_rows + delta.skipped_rows,
        expected.generation,
        expected.fence_token,
    )
    return StandaloneCrmAtomicUnitCommit(page.envelope, mutation, expected, proposed, delta)


def strict_row_id(value: str) -> int:
    return _strict_row_id(value)


def _strict_row_id(value: str) -> int:
    if not value or not value.isascii() or not value.isdecimal() or value.startswith("0"):
        raise ValueError("CRM row ID must be a canonical positive decimal string")
    parsed = int(value)
    if parsed < 1:
        raise ValueError("CRM row ID must be positive")
    return parsed


def _row_payload(row: CrmContact) -> dict[str, object]:
    return {
        "id": row.id,
        "kind": row.kind,
        "full_name": row.full_name,
        "phones": list(row.phones),
        "emails": list(row.emails),
        "observed_at": row.observed_at.isoformat() if row.observed_at is not None else None,
        "company_id": row.company_id,
    }


def _digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
