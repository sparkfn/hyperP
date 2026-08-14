"""Typed contracts for bounded CRM stage-history persistence and recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeGuard

from src.connectors.bitrix_stage_history.models import StageHistoryItem

StageHistoryReplayRunType = Literal["bounded_smoke_replay", "capture_failure_accounting"]
StageHistoryTerminalDisposition = Literal[
    "malformed_excluded",
    "capture_rejected_valid",
    "excluded_out_of_scope",
    "canonical_effective",
    "canonical_pending_parent",
    "parent_waiting",
    "parent_ambiguous",
    "same_hash_replay",
    "differing_hash_conflict",
]
StageHistoryParseScope = Literal["malformed", "out_of_scope", "in_scope"]
StageHistoryIdentityHashState = Literal["new_variant", "existing_same_hash", "new_conflict_variant"]
StageHistoryAssociationState = Literal[
    "selected_active", "selected_pending_review", "waiting", "ambiguous", "rejected"
]
StageHistoryAuthorityState = Literal[
    "effective", "withheld_parent", "withheld_conflict", "rejected", "corrected"
]
StageHistoryRetryState = Literal[
    "none", "pending", "claimed", "resolved", "rejected", "quarantined"
]
StageHistoryRetryRecordState = Literal["pending", "claimed", "resolved", "rejected", "quarantined"]
StageHistoryReviewKind = Literal[
    "resolve_parent", "reject_parent", "resolve_conflict", "apply_correction"
]
StageHistoryReviewStatus = Literal["pending", "claimed", "completed", "failed", "superseded"]
StageHistoryOutboxState = Literal["pending", "claimed", "published", "failed", "superseded"]
StageHistoryOutboxReason = Literal[
    "initial_effective",
    "parent_changed",
    "conflict_withheld",
    "conflict_resolved",
    "correction",
    "rejection",
]
StageHistoryPersistOutcome = Literal["committed", "already_committed"]

REPLAY_TERMINAL_DISPOSITIONS: frozenset[StageHistoryTerminalDisposition] = frozenset(
    {
        "excluded_out_of_scope",
        "canonical_effective",
        "canonical_pending_parent",
        "parent_waiting",
        "parent_ambiguous",
        "same_hash_replay",
        "differing_hash_conflict",
    }
)
FAILURE_TERMINAL_DISPOSITIONS: frozenset[StageHistoryTerminalDisposition] = frozenset(
    {"malformed_excluded", "capture_rejected_valid"}
)


def stage_history_review_configuration_fingerprint(
    command_id: str,
    kind: StageHistoryReviewKind,
    authorization_reference: str,
    *,
    review_lease_seconds: int,
    retry_backoff_seconds: int,
) -> str:
    _require_text_values(
        ("command_id", command_id),
        ("authorization_reference", authorization_reference),
    )
    _require_positive(review_lease_seconds, "review_lease_seconds")
    _require_positive(retry_backoff_seconds, "retry_backoff_seconds")
    payload = json.dumps(
        {
            "authorization_reference_digest": "sha256:"
            + hashlib.sha256(authorization_reference.encode("utf-8")).hexdigest(),
            "command_id": command_id,
            "kind": kind,
            "retry_backoff_seconds": retry_backoff_seconds,
            "review_lease_seconds": review_lease_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class StageHistoryReplaySourceWindow:
    stage_ingestion_artifact_id: str
    artifact_manifest_hmac: str
    source_contract_uuid: str
    entity_type_id: str
    owner_artifact_id: str
    owner_manifest_digest: str
    stage_artifact_id: str
    qualification_evidence_digest: str
    canonical_hash_version: str
    traversal_contract: str
    configuration_digest: str
    limits_digest: str

    def __post_init__(self) -> None:
        _require_text_values(
            ("stage_ingestion_artifact_id", self.stage_ingestion_artifact_id),
            ("artifact_manifest_hmac", self.artifact_manifest_hmac),
            ("source_contract_uuid", self.source_contract_uuid),
            ("entity_type_id", self.entity_type_id),
            ("owner_artifact_id", self.owner_artifact_id),
            ("owner_manifest_digest", self.owner_manifest_digest),
            ("stage_artifact_id", self.stage_artifact_id),
            ("qualification_evidence_digest", self.qualification_evidence_digest),
            ("canonical_hash_version", self.canonical_hash_version),
            ("traversal_contract", self.traversal_contract),
            ("configuration_digest", self.configuration_digest),
            ("limits_digest", self.limits_digest),
        )
        _require_hex(self.artifact_manifest_hmac, "artifact_manifest_hmac")
        if self.canonical_hash_version != "bitrix-stage-history-v1":
            raise ValueError("unsupported canonical_hash_version")
        if self.traversal_contract != "bounded_spool_reconcile":
            raise ValueError("unsupported traversal_contract")


@dataclass(frozen=True, slots=True)
class StageHistoryFailureSourceWindow:
    failed_artifact_id: str
    manifest_hmac: str
    source_contract_uuid: str
    stage_artifact_id: str
    qualification_evidence_digest: str
    configuration_digest: str
    limits_digest: str

    def __post_init__(self) -> None:
        _require_text_values(
            ("failed_artifact_id", self.failed_artifact_id),
            ("manifest_hmac", self.manifest_hmac),
            ("source_contract_uuid", self.source_contract_uuid),
            ("stage_artifact_id", self.stage_artifact_id),
            ("qualification_evidence_digest", self.qualification_evidence_digest),
            ("configuration_digest", self.configuration_digest),
            ("limits_digest", self.limits_digest),
        )
        _require_hex(self.manifest_hmac, "manifest_hmac")


StageHistorySourceWindow = StageHistoryReplaySourceWindow | StageHistoryFailureSourceWindow


@dataclass(frozen=True, slots=True)
class StageHistoryValidObservation:
    occurrence_id: str
    artifact_id: str
    page_sequence: int
    row_sequence: int
    event_identity: str
    canonical_hash: str
    item: StageHistoryItem
    logical_parent_source_system: str
    logical_parent_source_record_id: str
    source_observed_at: datetime

    def __post_init__(self) -> None:
        _require_text_values(
            ("occurrence_id", self.occurrence_id),
            ("artifact_id", self.artifact_id),
            ("event_identity", self.event_identity),
            ("logical_parent_source_system", self.logical_parent_source_system),
            ("logical_parent_source_record_id", self.logical_parent_source_record_id),
        )
        _require_positive(self.page_sequence, "page_sequence")
        _require_positive(self.row_sequence, "row_sequence")
        _require_prefixed_digest(self.canonical_hash, "sha256:", "canonical_hash")
        _require_aware(self.source_observed_at, "source_observed_at")
        if self.item.created_time.tzinfo is None:
            raise ValueError("stage event_at must be timezone-aware")
        expected_parent_id = f"bitrix-crm-deal-{self.item.owner_id}"
        if expected_parent_id != self.logical_parent_source_record_id:
            raise ValueError("logical parent identity must match the CRM deal source identity")


@dataclass(frozen=True, slots=True)
class StageHistoryMalformedObservation:
    occurrence_id: str
    artifact_id: str
    page_sequence: int
    row_sequence: int
    canonical_raw_row_digest: str
    safe_error_code: str
    source_observed_at: datetime

    def __post_init__(self) -> None:
        _require_text_values(
            ("occurrence_id", self.occurrence_id),
            ("artifact_id", self.artifact_id),
            ("safe_error_code", self.safe_error_code),
        )
        _require_positive(self.page_sequence, "page_sequence")
        _require_positive(self.row_sequence, "row_sequence")
        _require_prefixed_digest(
            self.canonical_raw_row_digest, "sha256:", "canonical_raw_row_digest"
        )
        _require_aware(self.source_observed_at, "source_observed_at")


StageHistoryObservation = StageHistoryValidObservation | StageHistoryMalformedObservation


@dataclass(frozen=True, slots=True)
class StageHistoryOccurrence:
    observation: StageHistoryObservation
    disposition: StageHistoryTerminalDisposition
    parse_scope: StageHistoryParseScope
    identity_hash_state: StageHistoryIdentityHashState | None = None
    association_state: StageHistoryAssociationState | None = None
    authority_state: StageHistoryAuthorityState | None = None
    retry_state: StageHistoryRetryState = "none"

    def __post_init__(self) -> None:
        if isinstance(self.observation, StageHistoryMalformedObservation):
            self._require_failure_only("malformed_excluded", "malformed")
            return
        if self.disposition == "capture_rejected_valid":
            self._require_no_domain_dimensions()
            if self.parse_scope not in {"in_scope", "out_of_scope"}:
                raise ValueError("capture_rejected_valid must retain a valid-row scope")
            return
        if self.disposition == "excluded_out_of_scope":
            self._require_no_domain_dimensions()
            if self.parse_scope != "out_of_scope":
                raise ValueError("excluded rows must have out_of_scope parse/scope state")
            return
        if self.disposition in FAILURE_TERMINAL_DISPOSITIONS:
            raise ValueError("valid observations cannot use malformed_excluded")
        if self.parse_scope != "in_scope":
            raise ValueError("domain observations must be in_scope")
        if None in (self.identity_hash_state, self.association_state, self.authority_state):
            raise ValueError("domain observations require every orthogonal domain state")
        self._validate_domain_disposition()

    def _require_failure_only(
        self, disposition: StageHistoryTerminalDisposition, scope: StageHistoryParseScope
    ) -> None:
        if self.disposition != disposition or self.parse_scope != scope:
            raise ValueError("malformed observations require malformed terminal accounting")
        self._require_no_domain_dimensions()

    def _require_no_domain_dimensions(self) -> None:
        if (
            any(
                value is not None
                for value in (
                    self.identity_hash_state,
                    self.association_state,
                    self.authority_state,
                )
            )
            or self.retry_state != "none"
        ):
            raise ValueError("non-domain occurrences cannot carry domain state")

    def _validate_domain_disposition(self) -> None:
        expected_hash: dict[StageHistoryTerminalDisposition, StageHistoryIdentityHashState] = {
            "canonical_effective": "new_variant",
            "canonical_pending_parent": "new_variant",
            "parent_waiting": "new_variant",
            "parent_ambiguous": "new_variant",
            "same_hash_replay": "existing_same_hash",
            "differing_hash_conflict": "new_conflict_variant",
        }
        if self.identity_hash_state != expected_hash[self.disposition]:
            raise ValueError("terminal disposition and identity/hash state disagree")
        if self.disposition == "canonical_effective" and (
            self.association_state != "selected_active" or self.authority_state != "effective"
        ):
            raise ValueError("canonical_effective requires active association and authority")
        if self.disposition == "canonical_pending_parent" and (
            self.association_state != "selected_pending_review"
            or self.authority_state != "withheld_parent"
        ):
            raise ValueError("canonical_pending_parent requires withheld pending association")
        if self.disposition == "parent_waiting" and (
            self.association_state != "waiting" or self.authority_state != "withheld_parent"
        ):
            raise ValueError("parent_waiting requires withheld waiting association")
        if self.disposition in {"parent_ambiguous", "differing_hash_conflict"} and (
            self.authority_state != "withheld_conflict"
        ):
            raise ValueError("conflict dispositions must withhold authority")
        if self.disposition == "parent_ambiguous" and self.association_state != "ambiguous":
            raise ValueError("parent_ambiguous requires an ambiguous association")
        parent_retry = self.disposition in {
            "canonical_pending_parent",
            "parent_waiting",
            "parent_ambiguous",
        }
        if parent_retry != (self.retry_state == "pending"):
            raise ValueError("only unresolved parent dispositions require a pending retry")


@dataclass(frozen=True, slots=True)
class StageHistoryTerminalAccounting:
    malformed_excluded: int = 0
    capture_rejected_valid: int = 0
    excluded_out_of_scope: int = 0
    canonical_effective: int = 0
    canonical_pending_parent: int = 0
    parent_waiting: int = 0
    parent_ambiguous: int = 0
    same_hash_replay: int = 0
    differing_hash_conflict: int = 0

    def __post_init__(self) -> None:
        _require_counts(
            self.malformed_excluded,
            self.capture_rejected_valid,
            self.excluded_out_of_scope,
            self.canonical_effective,
            self.canonical_pending_parent,
            self.parent_waiting,
            self.parent_ambiguous,
            self.same_hash_replay,
            self.differing_hash_conflict,
        )

    @property
    def fetched(self) -> int:
        return (
            self.malformed_excluded
            + self.capture_rejected_valid
            + self.excluded_out_of_scope
            + self.canonical_effective
            + self.canonical_pending_parent
            + self.parent_waiting
            + self.parent_ambiguous
            + self.same_hash_replay
            + self.differing_hash_conflict
        )


@dataclass(frozen=True, slots=True)
class StageHistoryIdentityAccounting:
    new_variant: int = 0
    existing_same_hash: int = 0
    new_conflict_variant: int = 0

    def __post_init__(self) -> None:
        _require_counts(self.new_variant, self.existing_same_hash, self.new_conflict_variant)

    @property
    def total(self) -> int:
        return self.new_variant + self.existing_same_hash + self.new_conflict_variant


@dataclass(frozen=True, slots=True)
class StageHistoryAssociationAccounting:
    selected_active: int = 0
    selected_pending_review: int = 0
    waiting: int = 0
    ambiguous: int = 0
    rejected: int = 0

    def __post_init__(self) -> None:
        _require_counts(
            self.selected_active,
            self.selected_pending_review,
            self.waiting,
            self.ambiguous,
            self.rejected,
        )

    @property
    def total(self) -> int:
        return (
            self.selected_active
            + self.selected_pending_review
            + self.waiting
            + self.ambiguous
            + self.rejected
        )


@dataclass(frozen=True, slots=True)
class StageHistoryAuthorityAccounting:
    effective: int = 0
    withheld_parent: int = 0
    withheld_conflict: int = 0
    rejected: int = 0
    corrected: int = 0

    def __post_init__(self) -> None:
        _require_counts(
            self.effective,
            self.withheld_parent,
            self.withheld_conflict,
            self.rejected,
            self.corrected,
        )

    @property
    def total(self) -> int:
        return (
            self.effective
            + self.withheld_parent
            + self.withheld_conflict
            + self.rejected
            + self.corrected
        )


@dataclass(frozen=True, slots=True)
class StageHistoryRetryAccounting:
    none: int = 0
    pending: int = 0
    claimed: int = 0
    resolved: int = 0
    rejected: int = 0
    quarantined: int = 0

    def __post_init__(self) -> None:
        _require_counts(
            self.none,
            self.pending,
            self.claimed,
            self.resolved,
            self.rejected,
            self.quarantined,
        )

    @property
    def total(self) -> int:
        return (
            self.none
            + self.pending
            + self.claimed
            + self.resolved
            + self.rejected
            + self.quarantined
        )


@dataclass(frozen=True, slots=True)
class StageHistoryAccounting:
    terminal: StageHistoryTerminalAccounting
    identity: StageHistoryIdentityAccounting
    association: StageHistoryAssociationAccounting
    authority: StageHistoryAuthorityAccounting
    retry: StageHistoryRetryAccounting

    @classmethod
    def from_occurrences(
        cls, occurrences: tuple[StageHistoryOccurrence, ...]
    ) -> StageHistoryAccounting:
        def count(attribute: str, expected: str) -> int:
            return sum(getattr(item, attribute) == expected for item in occurrences)

        return cls(
            terminal=StageHistoryTerminalAccounting(
                malformed_excluded=count("disposition", "malformed_excluded"),
                capture_rejected_valid=count("disposition", "capture_rejected_valid"),
                excluded_out_of_scope=count("disposition", "excluded_out_of_scope"),
                canonical_effective=count("disposition", "canonical_effective"),
                canonical_pending_parent=count("disposition", "canonical_pending_parent"),
                parent_waiting=count("disposition", "parent_waiting"),
                parent_ambiguous=count("disposition", "parent_ambiguous"),
                same_hash_replay=count("disposition", "same_hash_replay"),
                differing_hash_conflict=count("disposition", "differing_hash_conflict"),
            ),
            identity=StageHistoryIdentityAccounting(
                new_variant=count("identity_hash_state", "new_variant"),
                existing_same_hash=count("identity_hash_state", "existing_same_hash"),
                new_conflict_variant=count("identity_hash_state", "new_conflict_variant"),
            ),
            association=StageHistoryAssociationAccounting(
                selected_active=count("association_state", "selected_active"),
                selected_pending_review=count("association_state", "selected_pending_review"),
                waiting=count("association_state", "waiting"),
                ambiguous=count("association_state", "ambiguous"),
                rejected=count("association_state", "rejected"),
            ),
            authority=StageHistoryAuthorityAccounting(
                effective=count("authority_state", "effective"),
                withheld_parent=count("authority_state", "withheld_parent"),
                withheld_conflict=count("authority_state", "withheld_conflict"),
                rejected=count("authority_state", "rejected"),
                corrected=count("authority_state", "corrected"),
            ),
            retry=StageHistoryRetryAccounting(
                none=count("retry_state", "none"),
                pending=count("retry_state", "pending"),
                claimed=count("retry_state", "claimed"),
                resolved=count("retry_state", "resolved"),
                rejected=count("retry_state", "rejected"),
                quarantined=count("retry_state", "quarantined"),
            ),
        )

    def __post_init__(self) -> None:
        domain_rows = self.identity.total
        if self.association.total != domain_rows or self.authority.total != domain_rows:
            raise ValueError(
                "identity, association, and authority accounting must partition equally"
            )
        if self.retry.total != self.terminal.fetched:
            raise ValueError("retry accounting must partition every fetched occurrence")


@dataclass(frozen=True, slots=True)
class StageHistoryReplayUnit:
    run_type: StageHistoryReplayRunType
    unit_id: str
    artifact_id: str
    page_sequence: int
    page_digest: str
    occurrences: tuple[StageHistoryOccurrence, ...]
    accounting: StageHistoryAccounting

    def __post_init__(self) -> None:
        _require_text_values(("unit_id", self.unit_id), ("artifact_id", self.artifact_id))
        _require_positive(self.page_sequence, "page_sequence")
        _require_prefixed_digest(self.page_digest, "sha256:", "page_digest")
        if len(self.occurrences) > 50:
            raise ValueError("one stage-history replay unit cannot exceed 50 rows")
        occurrence_ids = tuple(item.observation.occurrence_id for item in self.occurrences)
        row_sequences = tuple(item.observation.row_sequence for item in self.occurrences)
        if len(occurrence_ids) != len(set(occurrence_ids)):
            raise ValueError("replay-unit occurrence identities must be unique")
        if row_sequences != tuple(sorted(row_sequences)) or len(row_sequences) != len(
            set(row_sequences)
        ):
            raise ValueError("replay-unit row sequences must be sorted and unique")
        if any(
            item.observation.artifact_id != self.artifact_id
            or item.observation.page_sequence != self.page_sequence
            for item in self.occurrences
        ):
            raise ValueError("every occurrence must belong to the replay unit")
        allowed = (
            REPLAY_TERMINAL_DISPOSITIONS
            if self.run_type == "bounded_smoke_replay"
            else FAILURE_TERMINAL_DISPOSITIONS
        )
        if any(item.disposition not in allowed for item in self.occurrences):
            raise ValueError("run type contains an incompatible terminal disposition")
        if self.accounting != StageHistoryAccounting.from_occurrences(self.occurrences):
            raise ValueError("unit accounting must equal its immutable occurrence outcomes")


@dataclass(frozen=True, slots=True)
class StageHistoryAssociationDecision:
    decision_id: str
    event_identity: str
    state: StageHistoryAssociationState
    available_at: datetime
    logical_parent_source_system: str
    logical_parent_source_record_id: str
    selected_parent_source_record_pk: str | None = None
    review_command_id: str | None = None

    def __post_init__(self) -> None:
        _require_text_values(
            ("decision_id", self.decision_id),
            ("event_identity", self.event_identity),
            ("logical_parent_source_system", self.logical_parent_source_system),
            ("logical_parent_source_record_id", self.logical_parent_source_record_id),
        )
        _require_optional_text(
            ("selected_parent_source_record_pk", self.selected_parent_source_record_pk),
            ("review_command_id", self.review_command_id),
        )
        _require_aware(self.available_at, "available_at")
        selected = self.state in {"selected_active", "selected_pending_review"}
        if selected != (self.selected_parent_source_record_pk is not None):
            raise ValueError("only selected associations require a parent SourceRecord PK")


@dataclass(frozen=True, slots=True)
class StageHistoryAuthorityTransition:
    decision_id: str
    event_identity: str
    prior_state: StageHistoryAuthorityState | None
    next_state: StageHistoryAuthorityState
    prior_head_version: int
    next_head_version: int
    prior_authority_token: int
    next_authority_token: int
    available_at: datetime
    selected_variant_hash: str | None = None
    association_decision_id: str | None = None
    correction_of_decision_id: str | None = None
    review_command_id: str | None = None

    def __post_init__(self) -> None:
        _require_text_values(
            ("decision_id", self.decision_id), ("event_identity", self.event_identity)
        )
        _require_optional_text(
            ("selected_variant_hash", self.selected_variant_hash),
            ("association_decision_id", self.association_decision_id),
            ("correction_of_decision_id", self.correction_of_decision_id),
            ("review_command_id", self.review_command_id),
        )
        _require_non_negative(self.prior_head_version, "prior_head_version")
        _require_non_negative(self.prior_authority_token, "prior_authority_token")
        if self.next_head_version != self.prior_head_version + 1:
            raise ValueError("authority head version must advance exactly one")
        if self.next_authority_token != self.prior_authority_token + 1:
            raise ValueError("authority token must advance exactly one")
        _require_aware(self.available_at, "available_at")
        selected = self.next_state in {"effective", "corrected"}
        if selected != (
            self.selected_variant_hash is not None and self.association_decision_id is not None
        ):
            raise ValueError("effective authority requires one variant and association")
        if self.selected_variant_hash is not None:
            _require_prefixed_digest(self.selected_variant_hash, "sha256:", "selected_variant_hash")
        correction = self.next_state == "corrected"
        if correction != (self.correction_of_decision_id is not None):
            raise ValueError("corrected authority must reference the corrected decision")
        if correction and self.review_command_id is None:
            raise ValueError("corrected authority requires a durable review command")


@dataclass(frozen=True, slots=True)
class StageHistoryRetry:
    retry_id: str
    occurrence_id: str
    retry_sequence: int
    state: StageHistoryRetryRecordState
    reason_code: str
    attempt_count: int
    max_attempts: int
    lease_owner: str | None = None
    lease_attempt_id: str | None = None
    lease_stream_generation: int | None = None
    lease_fencing_token: int | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    review_command_id: str | None = None

    def __post_init__(self) -> None:
        _require_text_values(
            ("retry_id", self.retry_id),
            ("occurrence_id", self.occurrence_id),
            ("reason_code", self.reason_code),
        )
        _require_optional_text(
            ("lease_owner", self.lease_owner),
            ("lease_attempt_id", self.lease_attempt_id),
            ("review_command_id", self.review_command_id),
        )
        _require_positive(self.retry_sequence, "retry_sequence")
        _require_non_negative(self.attempt_count, "attempt_count")
        _require_positive(self.max_attempts, "max_attempts")
        if self.attempt_count > self.max_attempts:
            raise ValueError("retry attempt_count cannot exceed max_attempts")
        lease_values = (
            self.lease_owner,
            self.lease_attempt_id,
            self.lease_stream_generation,
            self.lease_fencing_token,
            self.claimed_at,
            self.lease_expires_at,
        )
        if self.state == "claimed":
            if any(value is None for value in lease_values):
                raise ValueError("claimed retries require complete lease ownership")
            stream_generation = self.lease_stream_generation
            fencing_token = self.lease_fencing_token
            claimed_at = self.claimed_at
            lease_expires_at = self.lease_expires_at
            _require_positive(stream_generation, "lease_stream_generation")
            _require_positive(fencing_token, "lease_fencing_token")
            _require_aware(claimed_at, "claimed_at")
            _require_aware(lease_expires_at, "lease_expires_at")
            if claimed_at is None or lease_expires_at is None:
                raise ValueError("claimed retries require timestamps")
            if lease_expires_at <= claimed_at:
                raise ValueError("retry lease expiry must follow claim time")
        elif any(value is not None for value in lease_values):
            raise ValueError("only claimed retries may carry a lease")


@dataclass(frozen=True, slots=True)
class StageHistoryReviewCommand:
    command_id: str
    kind: StageHistoryReviewKind
    status: StageHistoryReviewStatus
    event_identity: str
    reviewer_id: str
    available_at: datetime
    expected_head_version: int
    expected_authority_token: int
    expected_authority_state: StageHistoryAuthorityState
    expected_variant_set_digest: str
    retry_sequence: int | None = None
    selected_variant_hash: str | None = None
    selected_association_decision_id: str | None = None
    correction_of_decision_id: str | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        _require_text_values(
            ("command_id", self.command_id),
            ("event_identity", self.event_identity),
            ("reviewer_id", self.reviewer_id),
        )
        _require_non_negative(self.expected_head_version, "expected_head_version")
        _require_non_negative(self.expected_authority_token, "expected_authority_token")
        _require_prefixed_digest(
            self.expected_variant_set_digest,
            "sha256:",
            "expected_variant_set_digest",
        )
        _require_optional_text(
            ("selected_variant_hash", self.selected_variant_hash),
            ("selected_association_decision_id", self.selected_association_decision_id),
            ("correction_of_decision_id", self.correction_of_decision_id),
            ("failure_code", self.failure_code),
        )
        _require_aware(self.available_at, "available_at")
        if self.kind in {"resolve_parent", "reject_parent"}:
            _require_positive(self.retry_sequence, "retry_sequence")
            if self.expected_authority_state not in {
                "withheld_parent",
                "withheld_conflict",
            }:
                raise ValueError(
                    "parent review commands require withheld parent/conflict authority"
                )
            if self.selected_variant_hash is not None:
                raise ValueError("parent review commands cannot select a hash variant")
        elif self.kind == "resolve_conflict":
            if self.expected_authority_state != "withheld_conflict":
                raise ValueError("conflict review commands require withheld-conflict authority")
        if self.kind not in {"resolve_parent", "reject_parent"} and self.retry_sequence is not None:
            raise ValueError("only parent review commands may select a retry sequence")
        if self.kind in {"resolve_parent", "reject_parent"} and (
            self.selected_association_decision_id is not None
        ):
            raise ValueError("parent review commands resolve their own association")
        if self.selected_variant_hash is not None:
            _require_prefixed_digest(self.selected_variant_hash, "sha256:", "selected_variant_hash")
        if self.kind in {"resolve_conflict", "apply_correction"} and (
            self.selected_variant_hash is None
        ):
            raise ValueError("variant review commands require an explicit selected hash")
        if self.kind == "apply_correction" and self.correction_of_decision_id is None:
            raise ValueError("correction commands require a prior decision")
        if self.kind == "apply_correction" and self.selected_association_decision_id is None:
            raise ValueError("correction commands require a selected association")
        if self.kind != "apply_correction" and self.correction_of_decision_id is not None:
            raise ValueError("only correction commands may reference a prior decision")
        if (self.status == "failed") != (self.failure_code is not None):
            raise ValueError("only failed review commands require a failure code")


@dataclass(frozen=True, slots=True)
class StageHistoryInvalidationIntent:
    intent_id: str
    authority_decision_id: str
    target_kind: Literal["crm_stage_timeline"]
    affected_logical_parent_digest: str
    reason: StageHistoryOutboxReason
    state: StageHistoryOutboxState
    sequence: int
    available_at: datetime
    lease_owner: str | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text_values(
            ("intent_id", self.intent_id),
            ("authority_decision_id", self.authority_decision_id),
        )
        _require_optional_text(("lease_owner", self.lease_owner))
        if self.target_kind != "crm_stage_timeline":
            raise ValueError("unsupported invalidation target kind")
        _require_prefixed_digest(
            self.affected_logical_parent_digest,
            "sha256:",
            "affected_logical_parent_digest",
        )
        _require_positive(self.sequence, "sequence")
        _require_aware(self.available_at, "available_at")
        lease_values = (self.lease_owner, self.claimed_at, self.lease_expires_at)
        if self.state == "claimed":
            if any(value is None for value in lease_values):
                raise ValueError("claimed invalidations require a complete lease")
            claimed_at = self.claimed_at
            lease_expires_at = self.lease_expires_at
            _require_aware(claimed_at, "claimed_at")
            _require_aware(lease_expires_at, "lease_expires_at")
            if claimed_at is None or lease_expires_at is None:
                raise ValueError("claimed invalidations require timestamps")
            if lease_expires_at <= claimed_at:
                raise ValueError("invalidation lease expiry must follow claim time")
        elif any(value is not None for value in lease_values):
            raise ValueError("only claimed invalidations may carry a lease")


@dataclass(frozen=True, slots=True)
class StageHistoryCheckpointSnapshot:
    run_type: StageHistoryReplayRunType
    source_window: StageHistorySourceWindow
    last_page_sequence: int | None
    revision: int
    committed_unit_count: int
    last_unit_id: str | None
    last_unit_digest: str | None
    accounting: StageHistoryAccounting

    def __post_init__(self) -> None:
        _require_non_negative(self.revision, "revision")
        _require_non_negative(self.committed_unit_count, "committed_unit_count")
        if self.run_type == "bounded_smoke_replay" and not isinstance(
            self.source_window, StageHistoryReplaySourceWindow
        ):
            raise ValueError("replay checkpoints require the replay source window")
        if self.run_type == "capture_failure_accounting" and not isinstance(
            self.source_window, StageHistoryFailureSourceWindow
        ):
            raise ValueError("failure checkpoints require the failed-artifact source window")
        initial = self.last_page_sequence is None
        if initial:
            if (
                any(
                    value != 0
                    for value in (
                        self.revision,
                        self.committed_unit_count,
                        self.accounting.terminal.fetched,
                    )
                )
                or self.last_unit_id is not None
                or self.last_unit_digest is not None
            ):
                raise ValueError("initial checkpoints cannot contain committed progress")
        else:
            _require_positive(self.last_page_sequence, "last_page_sequence")
            if self.revision != self.committed_unit_count or self.revision < 1:
                raise ValueError("checkpoint revision must equal committed unit count")
            if self.last_page_sequence != self.committed_unit_count:
                raise ValueError("artifact pages must commit contiguously from page one")
            if self.last_unit_id is None or self.last_unit_digest is None:
                raise ValueError("advanced checkpoints require the last committed unit identity")
            _require_prefixed_digest(self.last_unit_digest, "sha256:", "last_unit_digest")

    @property
    def phase(self) -> str:
        return (
            "crm_stage_history_artifact_replay_v1"
            if self.run_type == "bounded_smoke_replay"
            else "crm_stage_history_failed_artifact_v1"
        )

    @property
    def connector_version(self) -> str:
        return (
            "bitrix-crm-stagehistory-artifact-v1"
            if self.run_type == "bounded_smoke_replay"
            else "bitrix-crm-stagehistory-failed-artifact-v1"
        )

    @property
    def replay_boundary(self) -> str:
        return "exclusive_artifact_page_sequence"

    @property
    def schema_version(self) -> int:
        return 1


@dataclass(frozen=True, slots=True)
class StageHistoryUnitResult:
    outcome: StageHistoryPersistOutcome
    unit: StageHistoryReplayUnit
    checkpoint_before: StageHistoryCheckpointSnapshot
    checkpoint_after: StageHistoryCheckpointSnapshot
    association_decisions: tuple[StageHistoryAssociationDecision, ...] = ()
    authority_transitions: tuple[StageHistoryAuthorityTransition, ...] = ()
    retries: tuple[StageHistoryRetry, ...] = ()
    invalidation_intents: tuple[StageHistoryInvalidationIntent, ...] = ()

    def __post_init__(self) -> None:
        before = self.checkpoint_before
        after = self.checkpoint_after
        if before.run_type != self.unit.run_type or after.run_type != self.unit.run_type:
            raise ValueError("unit and checkpoint run types must match")
        if before.source_window != after.source_window:
            raise ValueError("a unit cannot change the authenticated source window")
        if self.outcome == "already_committed":
            if after != before:
                raise ValueError("already-committed replay cannot advance the checkpoint")
            if before.last_unit_id != self.unit.unit_id:
                raise ValueError("already-committed replay must match the checkpoint unit")
            if before.last_unit_digest != self.unit.page_digest:
                raise ValueError("already-committed replay digest does not match")
            return
        if self.unit.page_sequence != before.committed_unit_count + 1:
            raise ValueError("unit page must immediately follow the expected checkpoint")
        if (
            after.last_page_sequence != self.unit.page_sequence
            or after.revision != before.revision + 1
        ):
            raise ValueError("checkpoint must advance exactly one committed unit")
        if (
            after.last_unit_id != self.unit.unit_id
            or after.last_unit_digest != self.unit.page_digest
        ):
            raise ValueError("checkpoint must identify the committed unit")
        expected = _add_accounting(before.accounting, self.unit.accounting)
        if after.accounting != expected:
            raise ValueError("checkpoint accounting must advance by the unit accounting")
        if self.unit.run_type == "capture_failure_accounting" and any(
            (
                self.association_decisions,
                self.authority_transitions,
                self.retries,
                self.invalidation_intents,
            )
        ):
            raise ValueError("failed-capture accounting cannot create domain state")


@dataclass(frozen=True, slots=True)
class StageHistoryReconciliation:
    source_row_count: int
    occurrence_row_count: int
    committed_unit_count: int
    checkpoint: StageHistoryCheckpointSnapshot
    unit_ledger_accounting: StageHistoryAccounting
    distinct_variant_count: int
    source_record_count: int
    invalid_authority_head_count: int
    expected_invalidation_target_count: int
    actual_invalidation_target_count: int
    nonterminal_unit_count: int
    artifact_digest_mismatch_count: int
    error_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_counts(
            self.source_row_count,
            self.occurrence_row_count,
            self.committed_unit_count,
            self.distinct_variant_count,
            self.source_record_count,
            self.invalid_authority_head_count,
            self.expected_invalidation_target_count,
            self.actual_invalidation_target_count,
            self.nonterminal_unit_count,
            self.artifact_digest_mismatch_count,
        )
        if any(not value.strip() for value in self.error_codes):
            raise ValueError("reconciliation error codes must be non-empty")
        if len(self.error_codes) != len(set(self.error_codes)):
            raise ValueError("reconciliation error codes must be unique")

    @property
    def complete(self) -> bool:
        accounting = self.unit_ledger_accounting
        return (
            not self.error_codes
            and self.source_row_count == self.occurrence_row_count == accounting.terminal.fetched
            and self.committed_unit_count == self.checkpoint.committed_unit_count
            and self.checkpoint.accounting == accounting
            and self.distinct_variant_count == self.source_record_count
            and self.invalid_authority_head_count == 0
            and self.expected_invalidation_target_count == self.actual_invalidation_target_count
            and self.nonterminal_unit_count == 0
            and self.artifact_digest_mismatch_count == 0
        )


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_positive(value: int | None, field_name: str) -> None:
    if not _is_int(value) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_non_negative(value: int, field_name: str) -> None:
    if not _is_int(value) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _require_aware(value: datetime | None, field_name: str) -> None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_prefixed_digest(value: str, prefix: str, field_name: str) -> None:
    digest = value.removeprefix(prefix)
    if (
        not value.startswith(prefix)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ValueError(f"{field_name} must be a lowercase {prefix.rstrip(':')} digest")


def _require_text_values(*values: tuple[str, str]) -> None:
    for field_name, value in values:
        if not value.strip():
            raise ValueError(f"{field_name} must be non-empty")


def _require_optional_text(*values: tuple[str, str | None]) -> None:
    for field_name, value in values:
        if value is not None and not value.strip():
            raise ValueError(f"{field_name} must be non-empty when provided")


def _require_counts(*values: int) -> None:
    for value in values:
        if not _is_int(value) or value < 0:
            raise ValueError("accounting counts must be non-negative integers")


def _require_hex(value: str, field_name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a lowercase hexadecimal digest")


def _add_accounting(
    left: StageHistoryAccounting, right: StageHistoryAccounting
) -> StageHistoryAccounting:
    return StageHistoryAccounting(
        terminal=StageHistoryTerminalAccounting(
            malformed_excluded=left.terminal.malformed_excluded + right.terminal.malformed_excluded,
            capture_rejected_valid=left.terminal.capture_rejected_valid
            + right.terminal.capture_rejected_valid,
            excluded_out_of_scope=left.terminal.excluded_out_of_scope
            + right.terminal.excluded_out_of_scope,
            canonical_effective=left.terminal.canonical_effective
            + right.terminal.canonical_effective,
            canonical_pending_parent=left.terminal.canonical_pending_parent
            + right.terminal.canonical_pending_parent,
            parent_waiting=left.terminal.parent_waiting + right.terminal.parent_waiting,
            parent_ambiguous=left.terminal.parent_ambiguous + right.terminal.parent_ambiguous,
            same_hash_replay=left.terminal.same_hash_replay + right.terminal.same_hash_replay,
            differing_hash_conflict=left.terminal.differing_hash_conflict
            + right.terminal.differing_hash_conflict,
        ),
        identity=StageHistoryIdentityAccounting(
            new_variant=left.identity.new_variant + right.identity.new_variant,
            existing_same_hash=left.identity.existing_same_hash + right.identity.existing_same_hash,
            new_conflict_variant=left.identity.new_conflict_variant
            + right.identity.new_conflict_variant,
        ),
        association=StageHistoryAssociationAccounting(
            selected_active=left.association.selected_active + right.association.selected_active,
            selected_pending_review=left.association.selected_pending_review
            + right.association.selected_pending_review,
            waiting=left.association.waiting + right.association.waiting,
            ambiguous=left.association.ambiguous + right.association.ambiguous,
            rejected=left.association.rejected + right.association.rejected,
        ),
        authority=StageHistoryAuthorityAccounting(
            effective=left.authority.effective + right.authority.effective,
            withheld_parent=left.authority.withheld_parent + right.authority.withheld_parent,
            withheld_conflict=left.authority.withheld_conflict + right.authority.withheld_conflict,
            rejected=left.authority.rejected + right.authority.rejected,
            corrected=left.authority.corrected + right.authority.corrected,
        ),
        retry=StageHistoryRetryAccounting(
            none=left.retry.none + right.retry.none,
            pending=left.retry.pending + right.retry.pending,
            claimed=left.retry.claimed + right.retry.claimed,
            resolved=left.retry.resolved + right.retry.resolved,
            rejected=left.retry.rejected + right.retry.rejected,
            quarantined=left.retry.quarantined + right.retry.quarantined,
        ),
    )


def advance_stage_history_checkpoint(
    checkpoint: StageHistoryCheckpointSnapshot,
    unit: StageHistoryReplayUnit,
) -> StageHistoryCheckpointSnapshot:
    """Return the exact checkpoint state produced by committing one contiguous unit."""
    if checkpoint.run_type != unit.run_type:
        raise ValueError("unit and checkpoint run types must match")
    if unit.page_sequence != checkpoint.committed_unit_count + 1:
        raise ValueError("unit page must immediately follow the checkpoint")
    return StageHistoryCheckpointSnapshot(
        run_type=checkpoint.run_type,
        source_window=checkpoint.source_window,
        last_page_sequence=unit.page_sequence,
        revision=checkpoint.revision + 1,
        committed_unit_count=checkpoint.committed_unit_count + 1,
        last_unit_id=unit.unit_id,
        last_unit_digest=unit.page_digest,
        accounting=_add_accounting(checkpoint.accounting, unit.accounting),
    )
