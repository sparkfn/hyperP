"""Same-transaction identity-link append primitive for ingestion writers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from neo4j import ManagedTransaction

from src.graph.queries.identity_link_revisions import APPEND_IDENTITY_LINK_REVISIONS

IdentityLinkStatus = Literal[
    "resolved", "unresolved", "pending_review", "blocked", "rejected", "retired"
]
IdentityLinkResolutionKind = Literal[
    "baseline",
    "automatic_activation",
    "reviewed_activation",
    "review_rejection",
    "manual_no_match",
    "source_supersession",
    "person_merge",
    "person_unmerge",
    "person_retirement",
    "source_retirement",
]


@dataclass(frozen=True)
class IdentityLinkDesiredRevision:
    source_system: str
    source_instance_id: str
    source_entity_type: str
    source_entity_id: str
    identity_policy_version: str
    link_status: IdentityLinkStatus
    hyperp_person_id: str | None
    resolution_kind: IdentityLinkResolutionKind
    effective_at: str
    cause_key: str
    match_decision_id: str | None = None
    review_case_id: str | None = None


def identity_link_key(*parts: str) -> str:
    """Encode complete private source provenance without ambiguous delimiters."""
    if len(parts) != 5 or any(not part.strip() for part in parts):
        raise ValueError("identity-link provenance fields must not be blank")
    return "ilk1:" + "".join(f"{len(part)}:{part}" for part in parts)


def _validate(row: IdentityLinkDesiredRevision) -> None:
    if row.source_system != "bitrix_chat" or row.source_entity_type not in {
        "deal",
        "contact",
        "lead",
        "company",
    }:
        raise ValueError("identity-link source is not exportable")
    if (row.link_status == "resolved") != (row.hyperp_person_id is not None):
        raise ValueError("only resolved link state may contain a Person")
    if not row.cause_key.strip():
        raise ValueError("identity-link cause_key must not be blank")


def append_identity_link_revisions(
    tx: ManagedTransaction,
    rows: list[IdentityLinkDesiredRevision],
    *,
    skip_existing_heads: bool = False,
) -> list[tuple[str, int, int]]:
    """Append immutable revisions through the caller's existing transaction."""
    unique: dict[str, IdentityLinkDesiredRevision] = {}
    causes: set[str] = set()
    for row in rows:
        _validate(row)
        if row.cause_key in causes:
            continue
        causes.add(row.cause_key)
        key = identity_link_key(
            row.source_system,
            row.source_instance_id,
            row.source_entity_type,
            row.source_entity_id,
            row.identity_policy_version,
        )
        if key in unique:
            raise ValueError("multiple states for one identity link")
        unique[key] = row
    if not unique:
        return []
    payload = [{"link_key": key, **asdict(item)} for key, item in sorted(unique.items())]
    result = tx.run(
        APPEND_IDENTITY_LINK_REVISIONS, rows=payload, skip_existing_heads=skip_existing_heads
    )
    return [
        (
            str(record["event_id"]),
            int(record["global_revision"]),
            int(record["resolution_revision"]),
        )
        for record in result
    ]
