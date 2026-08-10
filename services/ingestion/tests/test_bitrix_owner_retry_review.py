"""Reviewed activity-owner retry resume contracts."""

from src.graph.queries.bitrix_backfill import RECORD_BITRIX_ACTIVITY_OWNER_RETRY
from src.pipeline_crm import _reviewed_owner_scope


def test_retry_recording_preserves_a_reviewed_exclusion() -> None:
    assert "WHEN retry.status = 'reviewed_excluded' THEN retry.status" in (
        RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    )
    assert "retry.status AS status" in RECORD_BITRIX_ACTIVITY_OWNER_RETRY


def test_retry_recording_reuses_reviewed_owner_evidence_within_generation() -> None:
    assert "(generation)-[:HAS_OWNER_RETRY]->(" in RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    assert "owner_deal_id: $owner_deal_id" in RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    assert "status: 'reviewed_excluded'" in RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    assert "reviewed_owner_retry IS NULL" in RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    assert "reviewed_owner_retry.source_identity <> $source_identity" in (
        RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    )
    assert "reviewed_owner_retry.source_boundary <> $source_boundary" in (
        RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    )
    assert (
        "min(reviewed_owner_retry.review_evidence_digest)"
        in RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    )
    assert "WHEN reviewed_owner_evidence_digest IS NOT NULL" in (
        RECORD_BITRIX_ACTIVITY_OWNER_RETRY
    )
    assert "retry.review_basis_evidence_digest" in RECORD_BITRIX_ACTIVITY_OWNER_RETRY


def test_reviewed_exclusion_resumes_as_out_of_scope() -> None:
    assert _reviewed_owner_scope("reviewed_excluded") == "out_of_scope"
    assert _reviewed_owner_scope("retryable") is None


def test_unknown_owner_review_status_is_rejected() -> None:
    try:
        _reviewed_owner_scope("unexpected")
    except RuntimeError as exc:
        assert str(exc) == "activity owner retry returned an invalid review status"
    else:
        raise AssertionError("unexpected owner review status was accepted")
