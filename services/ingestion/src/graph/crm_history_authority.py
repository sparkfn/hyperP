"""Typed façade over the fenced CRM-history authority ledger."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.queries.crm_history_authority import APPEND_CRM_HISTORY_AUTHORITY_DECISION

AuthorityDecisionKind = Literal["accepted", "variant", "parent", "correction"]


@dataclass(frozen=True)
class AuthorityWriteContext:
    logical_run_id: str
    ingest_run_id: str
    generation: int
    expected_head_version: int
    expected_fence_token: int
    next_fence_token: int

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("generation must be positive")
        if self.expected_head_version < 0 or self.expected_fence_token < 0:
            raise ValueError("expected authority CAS values cannot be negative")
        if self.next_fence_token <= self.expected_fence_token:
            raise ValueError("next_fence_token must advance the authority fence")


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

    def __post_init__(self) -> None:
        if self.decision_kind not in {"accepted", "variant", "parent", "correction"}:
            raise ValueError("unsupported CRM history authority decision kind")
        if self.hash_version != "bitrix-stage-history-v1":
            raise ValueError("unsupported CRM history hash version")
        if self.decision_kind == "correction" and self.correction_of_decision_id is None:
            raise ValueError("correction decisions require correction_of_decision_id")
        if self.decision_kind != "correction" and self.correction_of_decision_id is not None:
            raise ValueError("only correction decisions may reference a prior decision")


@dataclass(frozen=True)
class AuthorityAppendResult:
    decision_id: str
    head_version: int
    fence_token: int


def append_authority_decision(
    client: Neo4jClient,
    context: AuthorityWriteContext,
    decision: AuthorityDecision,
) -> AuthorityAppendResult | None:
    """Append a decision only when run generation and head CAS both match."""

    def _work(tx: ManagedTransaction) -> AuthorityAppendResult | None:
        row = tx.run(
            APPEND_CRM_HISTORY_AUTHORITY_DECISION,
            **context.__dict__,
            **decision.__dict__,
        ).single()
        if row is None:
            return None
        return AuthorityAppendResult(
            decision_id=str(row["decision_id"]),
            head_version=int(row["head_version"]),
            fence_token=int(row["fence_token"]),
        )

    with client.session() as session:
        return session.execute_write(_work)
