"""Aggregate-safe report types and final NRIC rehash reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

from src.graph.client import Neo4jClient
from src.nric_rehash_plan import (
    PlanReport,
    VerifyReport,
    build_verify_report,
)
from src.nric_rehash_state import (
    EvidenceCounts,
    MigrationState,
    read_current_revision,
    read_evidence_counts,
)


class CountSummary(TypedDict):
    total: int
    non_digest: int
    digest_shaped: int


class FinalCountSummary(TypedDict):
    identifier_count: int
    non_digest_count: int


class ApplyReport(TypedDict):
    command: Literal["apply"]
    status: Literal["ok", "blocked"]
    phase: str
    changes: int
    batches: int
    converted_in_place: int
    converted_consolidated: int
    removed_nodes: int
    already_digest: int
    unprocessable: dict[str, int]
    digest_metadata_gap: int
    baseline: CountSummary
    final: FinalCountSummary
    evidence_baseline: EvidenceCounts
    evidence_final: EvidenceCounts
    count_equation_ok: bool
    conversion_equation_ok: bool
    evidence_equation_ok: bool
    attribution_unchanged: bool
    revision_unchanged: bool


@dataclass(frozen=True)
class _Reconciliation:
    count_equation_ok: bool
    conversion_equation_ok: bool
    evidence_equation_ok: bool
    attribution_unchanged: bool
    revision_unchanged: bool


def build_final_report(
    client: Neo4jClient,
    state: MigrationState,
    plan: PlanReport,
    key_version: str,
    *,
    changes: int,
) -> tuple[ApplyReport, int]:
    verification = build_verify_report(client, key_version)
    evidence = client.execute_read(read_evidence_counts)
    reconciliation = _reconcile(
        state,
        verification,
        evidence,
        read_current_revision(client),
    )
    return _build_apply_report(state, plan, verification, evidence, changes, reconciliation)


def _reconcile(
    state: MigrationState,
    verification: VerifyReport,
    evidence: EvidenceCounts,
    current_revision: int | None,
) -> _Reconciliation:
    return _Reconciliation(
        count_equation_ok=(
            verification["identifier_count"] == state.baseline_total - state.removed_nodes
        ),
        conversion_equation_ok=state.baseline_non_digest
        == (
            state.converted_in_place
            + state.converted_consolidated
            + verification["non_digest_count"]
        ),
        evidence_equation_ok=(
            evidence["asserts"] == state.baseline_asserts
            and evidence["identified_by"] == state.baseline_identified_by
            and evidence["anchored_by"] == state.baseline_anchored_by
            and evidence["moved"] == state.baseline_moved
        ),
        attribution_unchanged=evidence["active_links"] == state.baseline_active_links,
        revision_unchanged=current_revision == state.baseline_revision,
    )


def _build_apply_report(
    state: MigrationState,
    plan: PlanReport,
    verification: VerifyReport,
    evidence: EvidenceCounts,
    changes: int,
    reconciliation: _Reconciliation,
) -> tuple[ApplyReport, int]:
    blocked = _verification_is_blocked(plan, verification, reconciliation)
    report = ApplyReport(
        command="apply",
        status="blocked" if blocked else "ok",
        phase=state.phase,
        changes=changes,
        batches=state.batches,
        converted_in_place=state.converted_in_place,
        converted_consolidated=state.converted_consolidated,
        removed_nodes=state.removed_nodes,
        already_digest=plan["already_digest"],
        unprocessable=dict(plan["unprocessable"]),
        digest_metadata_gap=plan["digest_metadata_gap"],
        baseline={
            "total": state.baseline_total,
            "non_digest": state.baseline_non_digest,
            "digest_shaped": state.baseline_digest_shaped,
        },
        final={
            "identifier_count": verification["identifier_count"],
            "non_digest_count": verification["non_digest_count"],
        },
        evidence_baseline=EvidenceCounts(
            asserts=state.baseline_asserts,
            identified_by=state.baseline_identified_by,
            anchored_by=state.baseline_anchored_by,
            moved=state.baseline_moved,
            has_identifier=state.baseline_has_identifier,
            active_links=state.baseline_active_links,
        ),
        evidence_final=evidence,
        count_equation_ok=reconciliation.count_equation_ok,
        conversion_equation_ok=reconciliation.conversion_equation_ok,
        evidence_equation_ok=reconciliation.evidence_equation_ok,
        attribution_unchanged=reconciliation.attribution_unchanged,
        revision_unchanged=reconciliation.revision_unchanged,
    )
    return report, 1 if blocked else 0


def _verification_is_blocked(
    plan: PlanReport,
    verification: VerifyReport,
    reconciliation: _Reconciliation,
) -> bool:
    return (
        plan["convertible"] > 0
        or sum(plan["unprocessable"].values()) > 0
        or not verification["ok"]
        or not reconciliation.count_equation_ok
        or not reconciliation.conversion_equation_ok
        or not reconciliation.evidence_equation_ok
        or not reconciliation.attribution_unchanged
        or not reconciliation.revision_unchanged
    )
