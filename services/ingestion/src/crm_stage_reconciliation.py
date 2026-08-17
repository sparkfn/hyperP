"""Aggregate reconciliation contracts for the #148 authoritative stage release."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class CrmStageReconciliationReport:
    occurrence_count: int
    distinct_occurrence_count: int
    nonterminal_occurrence_count: int
    variant_count: int
    variant_identity_count: int
    authority_head_count: int
    missing_head_decision_count: int
    invalid_selected_authority_count: int
    unresolved_retry_count: int
    quarantined_retry_count: int
    invalidation_count: int
    unpublished_invalidation_count: int
    complete: bool
    error_codes: tuple[str, ...]
    digest: str

    @classmethod
    def create(cls, **counts: int) -> CrmStageReconciliationReport:
        error_codes: list[str] = []
        if counts["occurrence_count"] != counts["distinct_occurrence_count"]:
            error_codes.append("duplicate_occurrence_identity")
        for key in (
            "nonterminal_occurrence_count",
            "missing_head_decision_count",
            "invalid_selected_authority_count",
            "unresolved_retry_count",
            "unpublished_invalidation_count",
        ):
            if counts[key]:
                error_codes.append(key.removesuffix("_count"))
        provisional = cls(
            **counts,
            complete=not error_codes,
            error_codes=tuple(sorted(error_codes)),
            digest="",
        )
        return cls(**{**asdict(provisional), "digest": reconciliation_digest(provisional)})


@dataclass(frozen=True, slots=True)
class CrmStageInvalidationStatus:
    total: int
    pending: int
    claimed: int
    published: int
    failed: int
    superseded: int
    active_projection_count: int
    projected_parent_count: int
    active_mapping_versions: tuple[str, ...]
    active_policy_versions: tuple[str, ...]

    @property
    def rebuilt(self) -> bool:
        return self.pending == 0 and self.claimed == 0 and self.failed == 0


@dataclass(frozen=True, slots=True)
class CrmStageRebuildResult:
    mapping_version: str
    policy_version: str
    projection_count: int
    retired_count: int
    published_invalidation_count: int


@dataclass(frozen=True, slots=True)
class CrmStageReleaseStatus:
    enabled: bool
    mapping_version: str | None
    policy_version: str | None
    mapping_digest: str | None
    boundary_digest: str | None
    reconciliation_digest: str | None
    accepted_by: str | None
    accepted_at: str | None


def reconciliation_digest(report: CrmStageReconciliationReport) -> str:
    payload = asdict(report)
    payload.pop("digest", None)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
