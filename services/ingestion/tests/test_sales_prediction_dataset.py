"""Dataset determinism and integration tests for the #125 dataset (issue #125.2).

Proves that re-running the same accepted versions produces the same row
identities, labels, splits, and content digest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.sales_prediction.dataset import build_dataset, summarize_build
from src.sales_prediction.dataset_serialization import content_digest, write_dataset
from src.sales_prediction.models import DealVersion, ReleaseSnapshot, StageEvent

MAPPING = "crm-stage-map-2026-08-18-v1"
POLICY = "crm-stage-lifecycle-policy-2026-08-18-v1"
CUTOFF = datetime(2026, 8, 1, tzinfo=UTC)
ACCEPTED = datetime(2026, 8, 18, 10, 0, tzinfo=UTC)


def _release() -> ReleaseSnapshot:
    return ReleaseSnapshot(
        enabled=True,
        mapping_version=MAPPING,
        policy_version=POLICY,
        accepted_at=ACCEPTED,
        evidence_cutoff_at=CUTOFF,
        source_accounting_complete=True,
        analytical_release_consistent=True,
        restated_event_count=0,
    )


def _evidence() -> tuple:
    snapshot = datetime(2026, 1, 10, 8, 0, tzinfo=UTC)
    events = (
        StageEvent(
            event_identity="e1",
            parent_key=("bitrix_chat", "deal-1"),
            mapped_state="open",
            event_at=snapshot,
            available_at=snapshot,
            authority_head_version=1,
            category_id="5",
            stage_id="C5:NEW",
            source_semantic="S",
        ),
        StageEvent(
            event_identity="e2",
            parent_key=("bitrix_chat", "deal-1"),
            mapped_state="won",
            event_at=snapshot + timedelta(days=20),
            available_at=snapshot + timedelta(days=20),
            authority_head_version=1,
        ),
        StageEvent(
            event_identity="e3",
            parent_key=("bitrix_chat", "deal-2"),
            mapped_state="open",
            event_at=datetime(2026, 2, 1, tzinfo=UTC),
            available_at=datetime(2026, 2, 1, tzinfo=UTC),
            authority_head_version=1,
        ),
    )
    versions = (
        DealVersion(
            parent_key=("bitrix_chat", "deal-1"),
            version_key="4:abc:1",
            source_record_version=1,
            entity_key="eko",
            observed_at=datetime(2026, 1, 5, tzinfo=UTC),
            ingested_at=datetime(2026, 1, 5, tzinfo=UTC),
            activated_at=datetime(2026, 1, 5, tzinfo=UTC),
            superseded_at=None,
            rejected_at=None,
            link_failed_at=None,
            linked_person_count=1,
            active_person_count=1,
            latest_linked_at=datetime(2026, 1, 6, tzinfo=UTC),
            timestamps_valid=True,
            amount_state="known",
            currency_status="supported",
            lifecycle_status="active",
            amount_value=1200.0,
            currency="SGD",
            assigned_known=True,
            contact_count=2,
            linked_person_ids=("p1",),
            active_person_ids=("p1",),
        ),
        DealVersion(
            parent_key=("bitrix_chat", "deal-2"),
            version_key="4:def:1",
            source_record_version=1,
            entity_key="eko",
            observed_at=datetime(2026, 1, 28, tzinfo=UTC),
            ingested_at=datetime(2026, 1, 28, tzinfo=UTC),
            activated_at=datetime(2026, 1, 28, tzinfo=UTC),
            superseded_at=None,
            rejected_at=None,
            link_failed_at=None,
            linked_person_count=0,
            active_person_count=0,
            latest_linked_at=None,
            timestamps_valid=True,
            amount_state="missing",
            currency_status="missing",
            lifecycle_status="active",
        ),
    )
    from src.sales_prediction.models import SalesEvidence

    return SalesEvidence(
        release=_release(),
        events=events,
        versions=versions,
        invalid_event_parents=frozenset(),
    )


def test_build_dataset_produces_rows_only_for_positive_and_negative() -> None:
    evidence = _evidence()
    rows_by_entity = build_dataset(evidence, ("eko",))
    rows = rows_by_entity.get("eko", [])
    # deal-1: open then won within 30d -> positive
    # deal-2: open at 2026-02-01, no won -> negative (mature since cutoff is Aug)
    assert len(rows) == 2
    statuses = {r.label_status for r in rows}
    assert statuses == {"positive", "negative"}


def test_build_dataset_is_deterministic_across_runs() -> None:
    evidence = _evidence()
    first = build_dataset(evidence, ("eko",))
    second = build_dataset(evidence, ("eko",))
    first_rows = first.get("eko", [])
    second_rows = second.get("eko", [])
    assert [r.row_id for r in first_rows] == [r.row_id for r in second_rows]
    assert content_digest(first_rows) == content_digest(second_rows)


def test_write_dataset_produces_same_content_digest(tmp_path: Path) -> None:
    evidence = _evidence()
    rows = build_dataset(evidence, ("eko",)).get("eko", [])
    meta = {"schema": "issue-125-crm-dataset-v1"}
    d1 = write_dataset(tmp_path / "one.sqlite3", meta, rows)
    d2 = write_dataset(tmp_path / "two.sqlite3", meta, rows)
    assert d1.content_digest == d2.content_digest
    assert d1.row_count == d2.row_count


def test_summarize_build_counts_labels_correctly() -> None:
    evidence = _evidence()
    rows = build_dataset(evidence, ("eko",)).get("eko", [])
    from src.sales_prediction.dataset_serialization import DatasetDigest

    digest = DatasetDigest(
        row_count=len(rows),
        content_digest=content_digest(rows),
        file_sha256="0" * 64,
    )
    result = summarize_build("eko", rows, digest)
    assert result.row_count == 2
    assert result.positive_count == 1
    assert result.negative_count == 1


def test_row_id_is_deterministic_and_unique() -> None:
    evidence = _evidence()
    rows = build_dataset(evidence, ("eko",)).get("eko", [])
    ids = [r.row_id for r in rows]
    assert len(set(ids)) == len(ids)
    # re-build produces same ids
    rows2 = build_dataset(evidence, ("eko",)).get("eko", [])
    assert ids == [r.row_id for r in rows2]
