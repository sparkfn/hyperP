"""Exact terminal partition evidence for logical activity cleanup."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from intelligence.crm.activities.cleanup.types import (
    DISPOSITIONS,
    RECONCILIATION_SCHEMA,
    canonical_digest,
    exact_keys,
    require_count,
    require_digest,
    require_identifier,
    require_mapping,
)


@dataclass(frozen=True)
class Reconciliation:
    """Disjoint terminal accounting for every receipt-authorized identity."""

    receipt_digest: str
    outcomes: tuple[tuple[str, str], ...]
    failure_codes: tuple[tuple[str, str], ...]
    before_counts: tuple[tuple[str, int], ...]
    after_counts: tuple[tuple[str, int], ...]
    outcome_digest: str

    def __post_init__(self) -> None:
        require_digest(self.receipt_digest, "receipt_digest")
        if tuple(sorted(self.outcomes)) != self.outcomes or len(
            {key for key, _ in self.outcomes}
        ) != len(self.outcomes):
            raise ValueError("reconciliation outcomes are not canonical")
        if set(value for _, value in self.outcomes) - DISPOSITIONS:
            raise ValueError("reconciliation disposition is invalid")
        if tuple(sorted(self.failure_codes)) != self.failure_codes or set(
            key for key, _ in self.failure_codes
        ) - set(key for key, _ in self.outcomes):
            raise ValueError("reconciliation failure codes are invalid")
        for key, value in (*self.outcomes, *self.failure_codes):
            require_identifier(key, "reconciliation identity")
            require_identifier(value, "reconciliation value")
        for values, field in (
            (self.before_counts, "before counts"),
            (self.after_counts, "after counts"),
        ):
            if tuple(sorted(values)) != values:
                raise ValueError(f"{field} are not canonical")
            for key, count in values:
                require_identifier(key, field)
                require_count(count, field)
        require_digest(self.outcome_digest, "outcome_digest")
        if self.outcome_digest != canonical_digest(
            {"outcomes": dict(self.outcomes), "failure_codes": dict(self.failure_codes)}
        ):
            raise ValueError("reconciliation outcome digest is invalid")

    def as_dict(self) -> dict[str, object]:
        counts = {
            name: sum(1 for _, value in self.outcomes if value == name)
            for name in sorted(DISPOSITIONS)
        }
        return {
            "schema_version": RECONCILIATION_SCHEMA,
            "receipt_digest": self.receipt_digest,
            "outcomes": dict(self.outcomes),
            "failure_codes": dict(self.failure_codes),
            "before_counts": dict(self.before_counts),
            "after_counts": dict(self.after_counts),
            "counts": counts,
            "authorized_count": len(self.outcomes),
            "unexplained_remainder": 0,
            "outcome_digest": self.outcome_digest,
        }


def reconcile(
    receipt_digest: str,
    authorized_identities: Sequence[str],
    outcomes: Mapping[str, str],
    failure_codes: Mapping[str, str],
    before_counts: Mapping[str, int],
    after_counts: Mapping[str, int],
) -> Reconciliation:
    expected = tuple(sorted(authorized_identities))
    if tuple(sorted(outcomes)) != expected or len(expected) != len(set(expected)):
        raise RuntimeError("reconciliation does not cover the exact authorized identity set")
    if set(outcomes.values()) - DISPOSITIONS or set(failure_codes) - set(expected):
        raise RuntimeError("reconciliation has unknown outcomes")
    for identity, code in failure_codes.items():
        if outcomes[identity] not in {"failed", "retained", "conflict"}:
            raise RuntimeError("reconciliation failure code has successful outcome")
        require_identifier(code, "failure code")
    ordered = tuple((key, outcomes[key]) for key in expected)
    codes = tuple(sorted(failure_codes.items()))
    return Reconciliation(
        require_digest(receipt_digest, "receipt_digest"),
        ordered,
        codes,
        tuple(
            sorted(
                (key, require_count(value, "before count")) for key, value in before_counts.items()
            )
        ),
        tuple(
            sorted(
                (key, require_count(value, "after count")) for key, value in after_counts.items()
            )
        ),
        canonical_digest({"outcomes": dict(ordered), "failure_codes": dict(codes)}),
    )


def parse_reconciliation(value: object, authorized_identities: Sequence[str]) -> Reconciliation:
    raw = require_mapping(value, "cleanup reconciliation")
    expected = frozenset(
        {
            "schema_version",
            "receipt_digest",
            "outcomes",
            "failure_codes",
            "before_counts",
            "after_counts",
            "counts",
            "authorized_count",
            "unexplained_remainder",
            "outcome_digest",
        }
    )
    exact_keys(raw, expected, "cleanup reconciliation")
    if raw["schema_version"] != RECONCILIATION_SCHEMA:
        raise ValueError("cleanup reconciliation schema is unsupported")
    mappings = tuple(
        require_mapping(raw[key], key)
        for key in ("outcomes", "failure_codes", "before_counts", "after_counts", "counts")
    )
    outcomes, failures, before, after, counts = mappings
    expected_ids = tuple(sorted(authorized_identities))
    if tuple(outcomes) != expected_ids or raw["authorized_count"] != len(expected_ids):
        raise ValueError("cleanup reconciliation is incomplete")
    if raw["unexplained_remainder"] != 0 or set(counts) != DISPOSITIONS:
        raise ValueError("cleanup reconciliation is unbalanced")
    if any(
        counts[name] != sum(1 for item in outcomes.values() if item == name)
        for name in DISPOSITIONS
    ):
        raise ValueError("cleanup reconciliation counts are unbalanced")
    return Reconciliation(
        require_digest(raw["receipt_digest"], "receipt_digest"),
        tuple((key, require_identifier(item, "outcome")) for key, item in outcomes.items()),
        tuple((key, require_identifier(item, "failure code")) for key, item in failures.items()),
        tuple((key, require_count(value, "before count")) for key, value in before.items()),
        tuple((key, require_count(value, "after count")) for key, value in after.items()),
        require_digest(raw["outcome_digest"], "outcome_digest"),
    )
