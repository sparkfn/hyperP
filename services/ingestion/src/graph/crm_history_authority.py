"""Typed façade over the fenced CRM-history authority ledger."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction

from src.graph.client import Neo4jClient
from src.graph.queries.crm_history_authority import APPEND_CRM_HISTORY_AUTHORITY_DECISION


@dataclass(frozen=True)
class AuthorityWriteContext:
    logical_run_id: str
    ingest_run_id: str
    generation: int
    expected_head_version: int
    expected_fence_token: int
    next_fence_token: int


@dataclass(frozen=True)
class AuthorityDecision:
    decision_id: str
    event_identity: str
    canonical_hash: str
    hash_version: str
    decision_kind: str
    available_at: str
    logical_parent_source_system: str
    logical_parent_source_record_id: str
    correction_of_decision_id: str | None = None


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
