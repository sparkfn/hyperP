"""Evaluation report generator for the #125 CRM win MVP (issue #125.3).

Produces aggregate + per-month + per-sufficiency-band breakdowns and
a sealed evaluation artifact. All rendered output is aggregates only:
no Person/deal/event IDs, raw payloads, or restricted boundary values.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from src.sales_prediction.contracts import EVALUATION_SCHEMA_VERSION
from src.sales_prediction.evaluation.folds import build_temporal_folds, fold_rows
from src.sales_prediction.evaluation.metrics import (
    BootstrapResult,
    bootstrap_metric,
    compute_binary_metrics,
    precision_at_capacity,
)
from src.sales_prediction.evaluation.rules import (
    random_baseline_probabilities,
    rules_v1_probabilities,
)
from src.sales_prediction.models import DatasetRow, TemporalFold

if TYPE_CHECKING:
    from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
    from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore

_DEFAULT_CAPACITY_FRACTION = 0.10


@dataclass(frozen=True)
class FoldMetrics:
    """Metrics for one fold and one candidate."""

    test_month: str
    candidate: str
    precision_at_capacity: float
    lift_at_capacity: float
    pr_auc: float
    brier: float
    test_count: int
    positive_count: int
    capacity: int


@dataclass(frozen=True)
class AggregateMetrics:
    """Aggregate held-out metrics for one candidate."""

    candidate: str
    precision_at_capacity: float
    lift_at_capacity: float
    pr_auc: float
    brier: float
    log_loss: float
    ece: float
    total_test_count: int
    total_positive_count: int
    bootstrap_precision: BootstrapResult


@dataclass(frozen=True)
class SufficiencyBreakdown:
    """Per-sufficiency-band metrics for one candidate."""

    sufficiency: str
    test_count: int
    positive_count: int
    precision_at_capacity: float


@dataclass(frozen=True)
class EvaluationReport:
    """Full evaluation report: aggregate + per-fold + per-sufficiency."""

    schema_version: str
    entity_key: str
    fold_count: int
    aggregate: tuple[AggregateMetrics, ...]
    per_fold: tuple[FoldMetrics, ...]
    per_sufficiency: tuple[SufficiencyBreakdown, ...]


def evaluate_dataset(
    rows: list[DatasetRow],
    entity_key: str,
    *,
    capacity_fraction: float = _DEFAULT_CAPACITY_FRACTION,
) -> EvaluationReport:
    """Run full temporal evaluation on one population's dataset rows."""
    folds = build_temporal_folds(rows)
    all_test_rows: list[DatasetRow] = []
    fold_metrics: list[FoldMetrics] = []

    for fold in folds:
        train, test = fold_rows(fold, rows)
        all_test_rows.extend(test)

        for candidate_name, probs in _candidate_probabilities(train, test):
            metrics = compute_binary_metrics(test, probs, capacity_fraction=capacity_fraction)
            fold_metrics.append(
                FoldMetrics(
                    test_month=fold.test_month,
                    candidate=candidate_name,
                    precision_at_capacity=metrics.precision_at_capacity,
                    lift_at_capacity=metrics.lift_at_capacity,
                    pr_auc=metrics.pr_auc,
                    brier=metrics.brier,
                    test_count=metrics.test_count,
                    positive_count=metrics.positive_count,
                    capacity=metrics.capacity,
                )
            )

    aggregate = _aggregate_metrics(all_test_rows, folds, rows, capacity_fraction)
    per_sufficiency = _sufficiency_breakdown(all_test_rows, folds, rows, capacity_fraction)

    return EvaluationReport(
        schema_version=EVALUATION_SCHEMA_VERSION,
        entity_key=entity_key,
        fold_count=len(folds),
        aggregate=tuple(aggregate),
        per_fold=tuple(fold_metrics),
        per_sufficiency=tuple(per_sufficiency),
    )


def report_to_json(report: EvaluationReport) -> str:
    """Serialize an evaluation report to canonical JSON (aggregates only)."""
    return json.dumps(_report_to_dict(report), sort_keys=True, indent=2, ensure_ascii=False)


def seal_evaluation(
    store: LocalRestrictedArtifactStore,
    entity_key: str,
    report: EvaluationReport,
    *,
    provenance: ArtifactProvenanceInput,
    retention_days: int = 365,
) -> str:
    """Seal the evaluation report in the restricted store."""
    from datetime import UTC, datetime, timedelta

    report_json = report_to_json(report)
    with store.begin(artifact_kind="sales-evaluation") as session:
        session.write_bytes(f"{entity_key}_evaluation.json", report_json.encode("utf-8"))
        manifest = session.seal(
            metadata={
                "evaluation_schema_version": EVALUATION_SCHEMA_VERSION,
                "entity_key": entity_key,
                "fold_count": str(report.fold_count),
            },
            provenance=provenance,
            retention_expires_at=datetime.now(UTC) + timedelta(days=retention_days),
        )
    return manifest.artifact_id


def _candidate_probabilities(
    train: list[DatasetRow], test: list[DatasetRow]
) -> list[tuple[str, dict[str, float]]]:
    """Compute probabilities for all baseline candidates."""
    return [
        ("random", random_baseline_probabilities(test)),
        ("rules_v1", rules_v1_probabilities(train, test)),
    ]


def _precision_float(
    rows: list[DatasetRow],
    probabilities: dict[str, float],
    *,
    capacity_fraction: float = _DEFAULT_CAPACITY_FRACTION,
) -> float:
    """Wrapper returning only the precision from precision_at_capacity."""
    prec, _ = precision_at_capacity(rows, probabilities, capacity_fraction=capacity_fraction)
    return prec


def _aggregate_metrics(
    all_test_rows: list[DatasetRow],
    folds: list[TemporalFold],
    all_rows: list[DatasetRow],
    capacity_fraction: float,
) -> list[AggregateMetrics]:
    """Compute aggregate held-out metrics for each candidate."""
    if not all_test_rows or not folds:
        return []
    # Use the last fold's train set for base rate (most recent)
    train_rows = [r for r in all_rows if r.row_id in folds[-1].train_row_ids]
    results: list[AggregateMetrics] = []
    for candidate_name, probs in _candidate_probabilities(train_rows, all_test_rows):
        metrics = compute_binary_metrics(all_test_rows, probs, capacity_fraction=capacity_fraction)
        boot = bootstrap_metric(
            all_test_rows,
            probs,
            _precision_float,
            capacity_fraction=capacity_fraction,
        )
        results.append(
            AggregateMetrics(
                candidate=candidate_name,
                precision_at_capacity=metrics.precision_at_capacity,
                lift_at_capacity=metrics.lift_at_capacity,
                pr_auc=metrics.pr_auc,
                brier=metrics.brier,
                log_loss=metrics.log_loss,
                ece=metrics.ece,
                total_test_count=metrics.test_count,
                total_positive_count=metrics.positive_count,
                bootstrap_precision=boot,
            )
        )
    return results


def _sufficiency_breakdown(
    all_test_rows: list[DatasetRow],
    folds: list[TemporalFold],
    all_rows: list[DatasetRow],
    capacity_fraction: float,
) -> list[SufficiencyBreakdown]:
    """Compute per-sufficiency-band precision for the rules_v1 candidate."""
    if not all_test_rows or not folds:
        return []
    train_rows = [r for r in all_rows if r.row_id in folds[-1].train_row_ids]
    _, rules_probs = _candidate_probabilities(train_rows, all_test_rows)[1]
    bands = ["sufficient", "limited", "insufficient"]
    results: list[SufficiencyBreakdown] = []
    for band in bands:
        band_rows = [r for r in all_test_rows if r.sufficiency == band]
        if not band_rows:
            results.append(
                SufficiencyBreakdown(
                    sufficiency=band,
                    test_count=0,
                    positive_count=0,
                    precision_at_capacity=0.0,
                )
            )
            continue
        prec, _ = precision_at_capacity(band_rows, rules_probs, capacity_fraction=capacity_fraction)
        results.append(
            SufficiencyBreakdown(
                sufficiency=band,
                test_count=len(band_rows),
                positive_count=sum(1 for r in band_rows if r.label == 1),
                precision_at_capacity=prec,
            )
        )
    return results


def _report_to_dict(report: EvaluationReport) -> dict[str, object]:
    """Convert report to a JSON-safe dict (aggregates only)."""
    return {
        "schema_version": report.schema_version,
        "entity_key": report.entity_key,
        "fold_count": report.fold_count,
        "aggregate": [_aggregate_to_dict(a) for a in report.aggregate],
        "per_fold": [_fold_to_dict(f) for f in report.per_fold],
        "per_sufficiency": [asdict(s) for s in report.per_sufficiency],
    }


def _aggregate_to_dict(a: AggregateMetrics) -> dict[str, object]:
    return {
        "candidate": a.candidate,
        "precision_at_capacity": round(a.precision_at_capacity, 6),
        "lift_at_capacity": round(a.lift_at_capacity, 6),
        "pr_auc": round(a.pr_auc, 6),
        "brier": round(a.brier, 6),
        "log_loss": round(a.log_loss, 6),
        "ece": round(a.ece, 6),
        "total_test_count": a.total_test_count,
        "total_positive_count": a.total_positive_count,
        "bootstrap_precision": {
            "point_estimate": round(a.bootstrap_precision.point_estimate, 6),
            "mean": round(a.bootstrap_precision.mean, 6),
            "lower": round(a.bootstrap_precision.lower, 6),
            "upper": round(a.bootstrap_precision.upper, 6),
        },
    }


def _fold_to_dict(f: FoldMetrics) -> dict[str, object]:
    return {
        "test_month": f.test_month,
        "candidate": f.candidate,
        "precision_at_capacity": round(f.precision_at_capacity, 6),
        "lift_at_capacity": round(f.lift_at_capacity, 6),
        "pr_auc": round(f.pr_auc, 6),
        "brier": round(f.brier, 6),
        "test_count": f.test_count,
        "positive_count": f.positive_count,
        "capacity": f.capacity,
    }
