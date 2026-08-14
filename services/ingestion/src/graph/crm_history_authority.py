"""Typed façade over the CRM-history authority ledger.

The in-transaction primitive assumes its caller has already locked and asserted the
complete Bitrix stream fence in the same transaction.  The public client wrapper is
retained for compatibility with existing callers that only need the authority CAS.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.crm_history_authority import APPEND_CRM_HISTORY_AUTHORITY_DECISION

AuthorityDecisionKind = Literal["accepted", "variant", "parent", "correction"]
AuthorityDecisionState = Literal[
    "effective",
    "withheld_parent",
    "withheld_conflict",
    "rejected",
    "corrected",
]

_ALLOWED_KIND_STATES: frozenset[tuple[AuthorityDecisionKind, AuthorityDecisionState]] = frozenset(
    {
        ("accepted", "effective"),
        ("variant", "withheld_conflict"),
        ("variant", "rejected"),
        ("parent", "withheld_parent"),
        ("parent", "rejected"),
        ("correction", "corrected"),
    }
)
_DEFAULT_STATE_BY_KIND: dict[AuthorityDecisionKind, AuthorityDecisionState] = {
    "accepted": "effective",
    "variant": "withheld_conflict",
    "parent": "withheld_parent",
    "correction": "corrected",
}


class AuthorityDecisionConflictError(RuntimeError):
    """The decision ID already exists with different immutable semantics."""


@dataclass(frozen=True, init=False)
class AuthorityWriteContext:
    logical_run_id: str
    ingest_run_id: str
    generation: int
    expected_head_version: int
    expected_authority_token: int
    next_authority_token: int

    def __init__(
        self,
        logical_run_id: str,
        ingest_run_id: str,
        generation: int,
        expected_head_version: int,
        expected_authority_token: int | None = None,
        next_authority_token: int | None = None,
        *,
        expected_fence_token: int | None = None,
        next_fence_token: int | None = None,
    ) -> None:
        """Accept authority-token names and the legacy fence-token keyword aliases."""
        object.__setattr__(self, "logical_run_id", logical_run_id)
        object.__setattr__(self, "ingest_run_id", ingest_run_id)
        object.__setattr__(self, "generation", generation)
        object.__setattr__(self, "expected_head_version", expected_head_version)
        object.__setattr__(
            self,
            "expected_authority_token",
            _resolve_token_alias(
                expected_authority_token,
                expected_fence_token,
                "expected_authority_token",
            ),
        )
        object.__setattr__(
            self,
            "next_authority_token",
            _resolve_token_alias(
                next_authority_token,
                next_fence_token,
                "next_authority_token",
            ),
        )
        self.__post_init__()

    def __post_init__(self) -> None:
        if not self.logical_run_id.strip() or not self.ingest_run_id.strip():
            raise ValueError("authority run identities must be non-empty")
        if isinstance(self.generation, bool) or self.generation < 1:
            raise ValueError("generation must be positive")
        if (
            isinstance(self.expected_head_version, bool)
            or isinstance(self.expected_authority_token, bool)
            or self.expected_head_version < 0
            or self.expected_authority_token < 0
        ):
            raise ValueError("expected authority CAS values cannot be negative")
        if (
            isinstance(self.next_authority_token, bool)
            or self.next_authority_token != self.expected_authority_token + 1
        ):
            raise ValueError("next_authority_token must advance exactly one")

    @property
    def expected_fence_token(self) -> int:
        """Deprecated compatibility alias for ``expected_authority_token``."""
        return self.expected_authority_token

    @property
    def next_fence_token(self) -> int:
        """Deprecated compatibility alias for ``next_authority_token``."""
        return self.next_authority_token


@dataclass(frozen=True)
class AuthorityDecision:
    decision_id: str
    event_identity: str
    canonical_hash: str
    hash_version: str
    decision_kind: AuthorityDecisionKind
    available_at: str
    logical_parent_source_system: str
    logical_parent_source_record_id: str
    correction_of_decision_id: str | None = None
    authority_state: AuthorityDecisionState | None = None
    association_decision_id: str | None = None
    expected_invalidation_target_count: int = 0
    expected_invalidation_target_digests: tuple[str, ...] = ()
    require_existing_variant: bool = False
    require_selected_association: bool = False
    review_command_id: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.decision_id,
            self.event_identity,
            self.canonical_hash,
            self.available_at,
            self.logical_parent_source_system,
            self.logical_parent_source_record_id,
        )
        if not all(value.strip() for value in required):
            raise ValueError("authority decision identities and timestamps must be non-empty")
        if self.hash_version != "bitrix-stage-history-v1":
            raise ValueError("unsupported CRM history hash version")
        try:
            available_at = datetime.fromisoformat(self.available_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("available_at must be an ISO-8601 timestamp") from exc
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        resolved_state = self.authority_state or _DEFAULT_STATE_BY_KIND[self.decision_kind]
        if (self.decision_kind, resolved_state) not in _ALLOWED_KIND_STATES:
            raise ValueError("unsupported CRM history authority decision kind/state")
        object.__setattr__(self, "authority_state", resolved_state)
        if self.decision_kind == "correction" and self.correction_of_decision_id is None:
            raise ValueError("correction decisions require correction_of_decision_id")
        if self.decision_kind != "correction" and self.correction_of_decision_id is not None:
            raise ValueError("only correction decisions may reference a prior decision")
        if self.correction_of_decision_id == self.decision_id:
            raise ValueError("correction decisions cannot correct themselves")
        if self.association_decision_id is not None and not self.association_decision_id.strip():
            raise ValueError("association_decision_id must be non-empty when provided")
        if self.review_command_id is not None and not self.review_command_id.strip():
            raise ValueError("review_command_id must be non-empty when provided")
        if (
            isinstance(self.expected_invalidation_target_count, bool)
            or self.expected_invalidation_target_count < 0
        ):
            raise ValueError("expected invalidation target count cannot be negative")
        if len(self.expected_invalidation_target_digests) != (
            self.expected_invalidation_target_count
        ) or len(set(self.expected_invalidation_target_digests)) != len(
            self.expected_invalidation_target_digests
        ):
            raise ValueError("expected invalidation target digests must be unique and exact")
        if any(
            not value.startswith("sha256:") or len(value) != 71
            for value in self.expected_invalidation_target_digests
        ):
            raise ValueError("expected invalidation targets must be SHA-256 digests")
        if not isinstance(self.require_existing_variant, bool) or not isinstance(
            self.require_selected_association, bool
        ):
            raise ValueError("authority precondition flags must be booleans")
        if self.require_selected_association and self.association_decision_id is None:
            raise ValueError("selected-association enforcement requires a decision ID")

    @property
    def resolved_authority_state(self) -> AuthorityDecisionState:
        state = self.authority_state
        if state is None:  # Defensive for deserialization that bypasses __post_init__.
            raise ValueError("authority decision state was not resolved")
        return state


@dataclass(frozen=True, init=False)
class AuthorityAppendResult:
    decision_id: str
    head_version: int
    authority_token: int
    replayed: bool

    def __init__(
        self,
        decision_id: str,
        head_version: int,
        authority_token: int | None = None,
        replayed: bool = False,
        *,
        fence_token: int | None = None,
    ) -> None:
        object.__setattr__(self, "decision_id", decision_id)
        object.__setattr__(self, "head_version", head_version)
        object.__setattr__(
            self,
            "authority_token",
            _resolve_token_alias(authority_token, fence_token, "authority_token"),
        )
        object.__setattr__(self, "replayed", replayed)
        if not self.decision_id.strip():
            raise ValueError("decision_id must be non-empty")
        if isinstance(self.head_version, bool) or self.head_version < 1:
            raise ValueError("head_version must be positive")
        if isinstance(self.authority_token, bool) or self.authority_token < 1:
            raise ValueError("authority_token must be positive")

    @property
    def fence_token(self) -> int:
        """Deprecated compatibility alias for ``authority_token``."""
        return self.authority_token


def append_authority_decision_in_transaction(
    tx: ManagedTransaction,
    context: AuthorityWriteContext,
    decision: AuthorityDecision,
) -> AuthorityAppendResult | None:
    """Append after the caller has asserted the complete stream fence on ``tx``.

    A same-ID, same-semantics decision is returned as an attempt-independent replay.
    A same-ID decision with different immutable semantics raises instead of being
    mistaken for a head compare-and-swap miss.
    """
    row = tx.run(
        APPEND_CRM_HISTORY_AUTHORITY_DECISION,
        logical_run_id=context.logical_run_id,
        ingest_run_id=context.ingest_run_id,
        generation=context.generation,
        expected_head_version=context.expected_head_version,
        expected_authority_token=context.expected_authority_token,
        next_authority_token=context.next_authority_token,
        decision_id=decision.decision_id,
        event_identity=decision.event_identity,
        canonical_hash=decision.canonical_hash,
        hash_version=decision.hash_version,
        decision_kind=decision.decision_kind,
        authority_state=decision.resolved_authority_state,
        available_at=decision.available_at,
        logical_parent_source_system=decision.logical_parent_source_system,
        logical_parent_source_record_id=decision.logical_parent_source_record_id,
        correction_of_decision_id=decision.correction_of_decision_id,
        association_decision_id=decision.association_decision_id,
        expected_invalidation_target_count=decision.expected_invalidation_target_count,
        expected_invalidation_target_digests=list(decision.expected_invalidation_target_digests),
        require_existing_variant=decision.require_existing_variant,
        require_selected_association=decision.require_selected_association,
        review_command_id=decision.review_command_id,
    ).single()
    if row is None:
        return None
    if not _record_bool(row, "semantic_match", default=True):
        raise AuthorityDecisionConflictError(
            "authority decision ID already exists with different immutable semantics"
        )
    return AuthorityAppendResult(
        decision_id=str(row["decision_id"]),
        head_version=int(row["head_version"]),
        authority_token=_record_token(row),
        replayed=_record_bool(row, "replayed", default=False),
    )


def append_authority_decision(
    client: Neo4jClient,
    context: AuthorityWriteContext,
    decision: AuthorityDecision,
) -> AuthorityAppendResult | None:
    """Reject standalone authority writes that cannot prove a complete stream fence."""
    del client, context, decision
    raise RuntimeError(
        "standalone authority writes are disabled; use a fenced repository transaction"
    )


def _resolve_token_alias(
    authority_token: int | None,
    legacy_fence_token: int | None,
    field_name: str,
) -> int:
    if authority_token is None and legacy_fence_token is None:
        raise ValueError(f"{field_name} is required")
    if (
        authority_token is not None
        and legacy_fence_token is not None
        and authority_token != legacy_fence_token
    ):
        raise ValueError(f"{field_name} conflicts with its legacy fence-token alias")
    if authority_token is not None:
        return authority_token
    if legacy_fence_token is None:
        raise ValueError(f"{field_name} is required")
    return legacy_fence_token


def _record_bool(record: Record, key: str, *, default: bool) -> bool:
    value: object = record.get(key, default)
    if not isinstance(value, bool):
        raise RuntimeError(f"authority query returned an invalid {key}")
    return value


def _record_token(record: Record) -> int:
    value: object = record.get("authority_token")
    if value is None:
        value = record.get("fence_token")
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError("authority query returned an invalid authority token")
    return value
