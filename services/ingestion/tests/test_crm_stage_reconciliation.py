from src.crm_stage_reconciliation import (
    CrmStageReconciliationReport,
    CrmStageRetryRetentionResult,
)


def test_reconciliation_requires_terminal_unique_and_published_state() -> None:
    report = CrmStageReconciliationReport.create(
        occurrence_count=10,
        distinct_occurrence_count=10,
        nonterminal_occurrence_count=0,
        variant_count=10,
        variant_identity_count=10,
        authority_head_count=10,
        missing_head_decision_count=0,
        invalid_selected_authority_count=0,
        unresolved_retry_count=0,
        quarantined_retry_count=0,
        invalidation_count=10,
        unpublished_invalidation_count=0,
    )
    assert report.complete is True
    assert report.error_codes == ()
    assert report.digest.startswith("sha256:")


def test_reconciliation_surfaces_every_release_blocker() -> None:
    report = CrmStageReconciliationReport.create(
        occurrence_count=10,
        distinct_occurrence_count=9,
        nonterminal_occurrence_count=1,
        variant_count=10,
        variant_identity_count=9,
        authority_head_count=9,
        missing_head_decision_count=1,
        invalid_selected_authority_count=1,
        unresolved_retry_count=1,
        quarantined_retry_count=2,
        invalidation_count=9,
        unpublished_invalidation_count=1,
    )
    assert report.complete is False
    assert set(report.error_codes) == {
        "duplicate_occurrence_identity",
        "nonterminal_occurrence",
        "missing_head_decision",
        "invalid_selected_authority",
        "unresolved_retry",
        "unpublished_invalidation",
    }


def test_retry_retention_completes_only_for_exact_drained_cohort() -> None:
    completed = CrmStageRetryRetentionResult(
        expected_count=5,
        retained_count=5,
        remaining_unresolved_count=0,
        quarantined_retry_count=5,
    )
    incomplete = CrmStageRetryRetentionResult(
        expected_count=5,
        retained_count=4,
        remaining_unresolved_count=1,
        quarantined_retry_count=4,
    )
    assert completed.complete is True
    assert incomplete.complete is False
