"""Typed row models for accepted CRM stage evidence (issue #125).

These mirror the Gate 1 evidence contract from issue #149 with two additions
the dataset needs: the raw category/stage identity on stage events, and the
parsed deal-version payload facts (amount value, currency, assignment and
contact indicators) extracted once at the read boundary so raw payloads never
travel past the repository layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

MappedState = Literal["open", "won", "lost"]

AmountState = Literal[
    "missing",
    "invalid",
    "zero",
    "known",
    "not_reconstructable",
    "unavailable",
]

CurrencyStatus = Literal[
    "missing",
    "invalid",
    "supported",
    "unsupported",
    "not_reconstructable",
    "unavailable",
]


@dataclass(frozen=True)
class StageEvent:
    """One authoritative stage-history projection row."""

    event_identity: str
    parent_key: tuple[str, str]
    mapped_state: MappedState
    event_at: datetime
    available_at: datetime
    authority_head_version: int
    category_id: str | None = None
    stage_id: str | None = None
    source_semantic: str | None = None


@dataclass(frozen=True)
class PayloadFacts:
    """Point-in-time-safe facts parsed from one deal version's raw payload."""

    amount_state: str
    currency_status: str
    amount_value: float | None = None
    currency: str | None = None
    assigned_known: bool = False
    contact_count: int = 0


@dataclass(frozen=True)
class DealVersion:
    """One immutable CRM deal version with its linkage and payload facts."""

    parent_key: tuple[str, str]
    version_key: str
    source_record_version: int
    entity_key: str | None
    observed_at: datetime | None
    ingested_at: datetime | None
    activated_at: datetime | None
    superseded_at: datetime | None
    rejected_at: datetime | None
    link_failed_at: datetime | None
    linked_person_count: int
    active_person_count: int
    latest_linked_at: datetime | None
    timestamps_valid: bool
    amount_state: str
    currency_status: str
    lifecycle_status: str
    amount_value: float | None = None
    currency: str | None = None
    assigned_known: bool = False
    contact_count: int = 0
    linked_person_ids: tuple[str, ...] = ()
    active_person_ids: tuple[str, ...] = ()

    def payload_facts(self) -> PayloadFacts:
        return PayloadFacts(
            amount_state=self.amount_state,
            currency_status=self.currency_status,
            amount_value=self.amount_value,
            currency=self.currency,
            assigned_known=self.assigned_known,
            contact_count=self.contact_count,
        )


@dataclass(frozen=True)
class ReleaseSnapshot:
    """The accepted analytical release the evidence was read against."""

    enabled: bool
    mapping_version: str
    policy_version: str
    accepted_at: datetime
    evidence_cutoff_at: datetime
    source_accounting_complete: bool
    analytical_release_consistent: bool
    restated_event_count: int


@dataclass(frozen=True)
class SalesEvidence:
    """All accepted-release evidence for one dataset build, in reading order."""

    release: ReleaseSnapshot
    events: tuple[StageEvent, ...]
    versions: tuple[DealVersion, ...]
    invalid_event_parents: frozenset[tuple[str, str]]


LabelStatus = Literal["positive", "negative", "censored", "unknown", "ineligible"]

SufficiencyBand = Literal["sufficient", "limited", "insufficient"]


@dataclass(frozen=True)
class LabelEvidence:
    """One retrospective open-episode label under the #149 selector contract.

    ``parent_key`` and ``person`` linkage values are private: they may exist
    inside restricted artifacts but must never be rendered in reports.
    """

    parent_key: tuple[str, str]
    snapshot_at: datetime
    month: str
    entity_key: str
    status: LabelStatus
    reason: str
    mature: bool
    person_linked: bool
    timestamp_valid: bool
    history_determinate: bool
    amount_state: str
    currency_status: str
    amount_reconstructable: bool


@dataclass(frozen=True)
class DatasetRow:
    """One deterministic dataset row: raw point-in-time facts at ``as_of_at``.

    Raw identifiers (``deal_key``, ``person_key``, ``stage_id``) are permitted
    only because the dataset lives in the restricted sealed store; anything
    rendered outside it is aggregates only. Feature transforms (one-hot
    vocabularies, standardization) are derived at training time and live in
    the model artifact, never here.
    """

    row_id: str
    entity_key: str
    deal_key: str
    as_of_at: str
    month: str
    label: int
    label_status: str
    label_reason: str
    sufficiency: str
    person_key: str | None = None
    stage_id: str | None = None
    category_id: str | None = None
    source_semantic: str | None = None
    deal_age_days: float = 0.0
    days_since_prev_event: float = 0.0
    prior_transition_count: int = 0
    prior_won_count: int = 0
    prior_lost_count: int = 0
    episode_index: int = 1
    amount_value: float | None = None
    amount_state: str = "not_reconstructable"
    currency_status: str = "not_reconstructable"
    currency: str | None = None
    amount_known: int = 0
    amount_nonzero: int = 0
    assigned_known: int = 0
    contact_count: int = 0
    person_linked_at_s: int = 0
    entity_version_age_days: float | None = None
    month_sin: float = 0.0
    month_cos: float = 0.0
    missingness_count: int = 0


@dataclass(frozen=True)
class TemporalFold:
    """One rolling-origin fold: train on months before ``test_month``.

    ``excluded_train_deal_keys``/``excluded_train_person_keys`` keep every
    Person and deal on exactly one side of the split.
    """

    test_month: str
    train_months: tuple[str, ...]
    train_row_ids: frozenset[str]
    test_row_ids: frozenset[str]
    excluded_train_deal_keys: frozenset[str]
    excluded_train_person_keys: frozenset[str]


@dataclass(frozen=True)
class DatasetDigest:
    """Reproducibility digests for one built dataset."""

    row_count: int
    content_digest: str
    file_sha256: str
