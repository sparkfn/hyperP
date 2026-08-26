"""Regression coverage for the identity-link readiness baseline."""

from __future__ import annotations

from src.graph.migrations import _baseline_status
from src.graph.queries.identity_link_revision_migrations import (
    ADVANCE_IDENTITY_LINK_BASELINE,
    ADVANCE_IDENTITY_LINK_PROVENANCE_BACKFILL,
    COMPLETE_IDENTITY_LINK_PROVENANCE_BACKFILL,
    LIST_IDENTITY_LINK_BASELINE_BATCH,
    LIST_IDENTITY_LINK_PROVENANCE_BACKFILL_BATCH,
)
from src.graph.queries.identity_link_revisions import APPEND_IDENTITY_LINK_REVISIONS
from src.identity_link_revisions import (
    IdentityLinkDesiredRevision,
    append_identity_link_revisions,
)


def test_legacy_bitrix_rows_without_new_provenance_are_backfilled_before_baseline() -> None:
    query = LIST_IDENTITY_LINK_PROVENANCE_BACKFILL_BATCH
    assert (
        "record.source_entity_type IS NULL OR record.source_entity_type <> source_entity_type"
        in query
    )
    assert "record.source_entity_id IS NULL OR record.source_entity_id <> source_entity_id" in query
    assert "record.identity_policy_version IS NULL" in query
    assert (
        "record.identity_link_key IS NULL OR record.identity_link_key <> identity_link_key" in query
    )
    assert "record.source_record_id =~ '^bitrix-crm-(deal|contact|lead|company)-[0-9]+$'" in query
    assert "record.raw_payload" not in query
    assert "identifier" not in query.lower()
    assert "after_source_record_pk" in query
    assert "after_source_record_pk" in ADVANCE_IDENTITY_LINK_PROVENANCE_BACKFILL
    assert "migration.lease_until = now + duration({seconds: $lease_seconds})" in (
        ADVANCE_IDENTITY_LINK_PROVENANCE_BACKFILL
    )
    assert "provenance_completed_at" in COMPLETE_IDENTITY_LINK_PROVENANCE_BACKFILL
    assert "migration.lease_until = now + duration({seconds: $lease_seconds})" in (
        COMPLETE_IDENTITY_LINK_PROVENANCE_BACKFILL
    )
    assert "record.identity_link_key IS NOT NULL" in LIST_IDENTITY_LINK_BASELINE_BATCH
    assert "migration.lease_until = now + duration({seconds: $lease_seconds})" in (
        ADVANCE_IDENTITY_LINK_BASELINE
    )


def test_historical_legacy_deal_remains_unresolved_after_safe_backfill() -> None:
    legacy_deal = {
        "record_type": "crm_deal",
        "source_entity_type": "deal",
        "lifecycle_status": "active",
        "link_status": "linked",
        "person_ids": ["legacy-person"],
    }
    assert _baseline_status(legacy_deal) == ("unresolved", None)


class _Transaction:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, query: str, **params: object) -> list[object]:
        assert query == APPEND_IDENTITY_LINK_REVISIONS
        self.calls.append(params)
        return []


def _desired(cause_key: str) -> IdentityLinkDesiredRevision:
    return IdentityLinkDesiredRevision(
        source_system="bitrix_chat",
        source_instance_id="legacy-default",
        source_entity_type="contact",
        source_entity_id="42",
        identity_policy_version="crm_contact_identity_v1",
        link_status="unresolved",
        hyperp_person_id=None,
        resolution_kind="baseline",
        effective_at="2026-08-26T00:00:00+00:00",
        cause_key=cause_key,
    )


def test_ingestion_append_empty_and_duplicate_cause_do_not_allocate_twice() -> None:
    transaction = _Transaction()
    assert append_identity_link_revisions(transaction, []) == []  # type: ignore[arg-type]
    assert transaction.calls == []
    assert (
        append_identity_link_revisions(  # type: ignore[arg-type]
            transaction, [_desired("legacy-cause"), _desired("legacy-cause")]
        )
        == []
    )
    assert len(transaction.calls) == 1
    rows = transaction.calls[0]["rows"]
    assert isinstance(rows, list) and len(rows) == 1
