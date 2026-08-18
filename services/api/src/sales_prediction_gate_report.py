"""Privacy-safe aggregation and fixed Gate 1 threshold evaluation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from src.sales_prediction_gate_models import (
    GateDecision,
    GateMetadata,
    GateRelease,
    GateReport,
    LabelEvidence,
    PopulationDecision,
    PopulationMetrics,
    ThresholdResult,
)


def build_gate_report(
    labels: list[LabelEvidence],
    release: GateRelease,
    entity_keys: tuple[str, ...],
    *,
    generated_at: str,
    selector_version: str,
    eligibility_version: str,
    restatement_version: str,
) -> GateReport:
    populations = tuple(
        _population(entity, [item for item in labels if item.entity_key == entity], release)
        for entity in sorted(entity_keys)
    )
    return GateReport(
        generated_at=generated_at,
        metadata=GateMetadata(
            report_schema_version="issue-149-crm-won-gate-v1",
            selector_version=selector_version,
            mapping_version=release.mapping_version,
            policy_version=release.policy_version,
            eligibility_version=eligibility_version,
            evidence_cutoff_status="accepted_release_derived_not_rendered",
            accepted_source_boundary_status=(
                "terminally_accounted" if release.source_accounting_complete else "incomplete"
            ),
            restatement_version=restatement_version,
            restatement_status=(
                "present_and_applied" if release.restated_event_count else "none_observed"
            ),
        ),
        populations=populations,
        monthly_counts=tuple(_monthly(labels, entity_keys)),
    )


def _population(
    entity: str, labels: list[LabelEvidence], release: GateRelease
) -> PopulationDecision:
    by_deal: defaultdict[tuple[str, str], list[LabelEvidence]] = defaultdict(list)
    for item in labels:
        by_deal[item.private_parent_key].append(item)
    matured_deals = {
        key
        for key, rows in by_deal.items()
        if any(row.status in {"positive", "negative"} for row in rows)
    }
    positive_deals = {
        key for key, rows in by_deal.items() if any(row.status == "positive" for row in rows)
    }
    negative_deals = matured_deals - positive_deals
    usable_months = len({item.month for item in labels if item.status in {"positive", "negative"}})
    determinate = sum(item.history_determinate for item in labels)
    timestamp_valid = sum(item.timestamp_valid for item in labels)
    linked = sum(item.person_linked for item in labels)
    data_quality_censored = sum(item.status == "censored" for item in labels)
    known_amount = sum(item.amount_state in {"known", "zero"} for item in labels)
    zero_amount = sum(item.amount_state == "zero" for item in labels)
    matured_count = len(matured_deals)
    metrics = PopulationMetrics(
        entity_key=entity,
        snapshot_count=len(labels),
        unique_deal_count=len(by_deal),
        matured_eligible_deals=matured_count,
        positive_deals=len(positive_deals),
        negative_deals=len(negative_deals),
        unknown_snapshots=sum(item.status == "unknown" for item in labels),
        censored_snapshots=sum(item.status == "censored" for item in labels),
        ineligible_snapshots=sum(item.status == "ineligible" for item in labels),
        selected_parent_ambiguity_snapshots=sum(
            item.reason == "selected_parent_ambiguity" for item in labels
        ),
        missing_parent_snapshots=sum(
            item.reason == "missing_parent_at_snapshot" for item in labels
        ),
        usable_months=usable_months,
        rolling_temporal_folds=max(0, usable_months - 1),
        positive_rate=(len(positive_deals) / matured_count if matured_count else None),
        analytically_determinate_rate=_rate(determinate, len(labels)),
        valid_timestamp_rate=_rate(timestamp_valid, len(labels)),
        deterministic_person_linkage_rate=_rate(linked, len(labels)),
        data_quality_unknown_censored_rate=_rate(data_quality_censored, len(labels)),
        amount_known_rate=_rate(known_amount, len(labels)),
        amount_zero_rate=_rate(zero_amount, len(labels)),
        amount_revision_availability="snapshot_versioned",
        optional_interaction_coverage="not_evaluated_non_blocking",
    )
    quality = _quality_thresholds(metrics, release)
    go = _volume_thresholds(metrics, go=True)
    rules = _volume_thresholds(metrics, go=False)
    if all(item.passed for item in (*quality, *go)):
        recommendation: GateDecision = "go"
    elif all(item.passed for item in (*quality, *rules)):
        recommendation = "rules_only"
    else:
        recommendation = "collect_more_data"
    thresholds = (*quality, *go, *rules)
    return PopulationDecision(
        entity_key=entity,
        recommendation=recommendation,
        metrics=metrics,
        thresholds=tuple(thresholds),
    )


def _quality_thresholds(
    metrics: PopulationMetrics, release: GateRelease
) -> tuple[ThresholdResult, ...]:
    return (
        _threshold(
            "accepted_boundary_source_accounting",
            "100% terminal",
            "100% terminal" if release.source_accounting_complete else "incomplete",
            release.source_accounting_complete,
        ),
        _rate_threshold(
            "analytically_determinate_histories", 0.95, metrics.analytically_determinate_rate
        ),
        _rate_threshold("valid_label_timestamps", 0.95, metrics.valid_timestamp_rate),
        _rate_threshold(
            "deterministic_person_linkage", 0.90, metrics.deterministic_person_linkage_rate
        ),
        _maximum_rate_threshold(
            "data_quality_unknown_or_censored",
            0.10,
            metrics.data_quality_unknown_censored_rate,
        ),
        _threshold(
            "release_identity_consistency",
            "no silent conflict",
            "pass" if release.analytical_release_consistent else "fail",
            release.analytical_release_consistent,
        ),
        _threshold(
            "selected_parent_ambiguity",
            "0 snapshots",
            str(metrics.selected_parent_ambiguity_snapshots),
            metrics.selected_parent_ambiguity_snapshots == 0,
        ),
        _threshold(
            "unexplained_parent_loss",
            "0 snapshots",
            str(metrics.missing_parent_snapshots),
            metrics.missing_parent_snapshots == 0,
        ),
    )


def _volume_thresholds(metrics: PopulationMetrics, *, go: bool) -> tuple[ThresholdResult, ...]:
    prefix = "go" if go else "rules_only"
    requirements = (
        (f"{prefix}_matured_eligible_deals", 2000 if go else 500, metrics.matured_eligible_deals),
        (f"{prefix}_qualifying_positive_deals", 200 if go else 50, metrics.positive_deals),
        (f"{prefix}_matured_negative_deals", 500 if go else 200, metrics.negative_deals),
        (f"{prefix}_usable_calendar_months", 6 if go else 3, metrics.usable_months),
    )
    rows = tuple(
        _threshold(name, f">={required}", str(observed), observed >= required)
        for name, required, observed in requirements
    )
    if not go:
        return rows
    return (
        *rows,
        _threshold(
            "go_rolling_temporal_folds",
            ">=2",
            str(metrics.rolling_temporal_folds),
            metrics.rolling_temporal_folds >= 2,
        ),
    )


def _monthly(
    labels: list[LabelEvidence], entity_keys: tuple[str, ...]
) -> list[dict[str, str | int | float | None]]:
    counts: defaultdict[tuple[str, str, str], int] = defaultdict(int)
    for item in labels:
        if item.entity_key in entity_keys:
            counts[(item.entity_key, item.month, item.status)] += 1
    rows: list[dict[str, str | int | float | None]] = []
    for entity, month in sorted({(key[0], key[1]) for key in counts}):
        positive = counts[(entity, month, "positive")]
        negative = counts[(entity, month, "negative")]
        mature = positive + negative
        rows.append(
            {
                "entity_key": entity,
                "month": month,
                "matured_eligible": mature,
                "positive": positive,
                "negative": negative,
                "unknown": counts[(entity, month, "unknown")],
                "censored": counts[(entity, month, "censored")],
                "ineligible": counts[(entity, month, "ineligible")],
                "positive_rate": positive / mature if mature else None,
            }
        )
    return rows


def report_as_dict(report: GateReport) -> dict[str, object]:
    return asdict(report)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _rate_threshold(name: str, required: float, observed: float) -> ThresholdResult:
    return _threshold(name, f">={required:.0%}", f"{observed:.2%}", observed >= required)


def _maximum_rate_threshold(name: str, required: float, observed: float) -> ThresholdResult:
    return _threshold(name, f"<={required:.0%}", f"{observed:.2%}", observed <= required)


def _threshold(name: str, required: str, observed: str, passed: bool) -> ThresholdResult:
    return ThresholdResult(name=name, required=required, observed=observed, passed=passed)
