"""Async same-transaction append primitive for API lifecycle writers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from neo4j import AsyncManagedTransaction

from src.graph.queries.identity_link_revisions import (
    APPEND_IDENTITY_LINK_REVISIONS,
    GET_AFFECTED_IDENTITY_LINK_HEADS,
    GET_RESOLVED_IDENTITY_LINK_HEADS_FOR_PERSON,
)
from src.identity_link_types import (
    IdentityLinkDesiredRevision,
    identity_link_key,
    validate_desired_revision,
)


async def append_identity_link_revisions(
    tx: AsyncManagedTransaction,
    rows: Iterable[IdentityLinkDesiredRevision],
    *,
    skip_existing_heads: bool = False,
) -> list[tuple[str, int, int]]:
    """Append validated projections using *tx*; never opens a second transaction."""
    unique: dict[str, IdentityLinkDesiredRevision] = {}
    causes: set[str] = set()
    for desired in rows:
        validate_desired_revision(desired)
        link_key = identity_link_key(
            desired.source_system,
            desired.source_instance_id,
            desired.source_entity_type,
            desired.source_entity_id,
            desired.identity_policy_version,
        )
        if desired.cause_key in causes:
            continue
        causes.add(desired.cause_key)
        if link_key in unique:
            raise ValueError(
                "one identity-link append batch cannot contain multiple states for one link"
            )
        unique[link_key] = desired
    if not unique:
        return []
    cypher_rows = [
        {
            "link_key": link_key,
            "cause_key": item.cause_key,
            "source_system": item.source_system,
            "source_instance_id": item.source_instance_id,
            "source_entity_type": item.source_entity_type,
            "source_entity_id": item.source_entity_id,
            "identity_policy_version": item.identity_policy_version,
            "link_status": item.link_status,
            "hyperp_person_id": item.hyperp_person_id,
            "resolution_kind": item.resolution_kind,
            "effective_at": item.effective_at,
            "match_decision_id": item.match_decision_id,
            "review_case_id": item.review_case_id,
        }
        for link_key, item in sorted(unique.items())
    ]
    result = await tx.run(
        APPEND_IDENTITY_LINK_REVISIONS, rows=cypher_rows, skip_existing_heads=skip_existing_heads
    )
    values: list[tuple[str, int, int]] = []
    async for record in result:
        values.append(
            (
                str(record["event_id"]),
                int(record["global_revision"]),
                int(record["resolution_revision"]),
            )
        )
    return values


async def append_merge_affected_revisions(
    tx: AsyncManagedTransaction,
    *,
    merge_event_id: str,
    absorbed_person_id: str,
    survivor_person_id: str | None,
    resolution_kind: Literal["person_merge", "person_unmerge"],
    cause_prefix: str,
    effective_at: str,
) -> list[tuple[str, int, int]]:
    """Append one state per exported head affected by a merge lifecycle event."""
    if resolution_kind not in {"person_merge", "person_unmerge"}:
        raise ValueError("invalid merge identity-link resolution kind")
    operation = "merge" if resolution_kind == "person_merge" else "unmerge"
    result = await tx.run(
        GET_AFFECTED_IDENTITY_LINK_HEADS,
        merge_event_id=merge_event_id,
        absorbed_person_id=absorbed_person_id,
        operation=operation,
        merge_cause_prefix=f"person-merge:{merge_event_id}:",
    )
    rows: list[IdentityLinkDesiredRevision] = []
    async for record in result:
        source_system = str(record["source_system"])
        source_instance_id = str(record["source_instance_id"])
        source_entity_type = str(record["source_entity_type"])
        source_entity_id = str(record["source_entity_id"])
        policy = str(record["identity_policy_version"])
        link_key = identity_link_key(
            source_system,
            source_instance_id,
            source_entity_type,
            source_entity_id,
            policy,
        )
        if record["link_key"] != link_key:
            raise RuntimeError("identity-link affected-head key mismatch")
        status: Literal["resolved", "pending_review"] = (
            "resolved" if resolution_kind == "person_merge" else "pending_review"
        )
        rows.append(
            IdentityLinkDesiredRevision(
                source_system=source_system,
                source_instance_id=source_instance_id,
                source_entity_type=source_entity_type,
                source_entity_id=source_entity_id,
                identity_policy_version=policy,
                link_status=status,
                hyperp_person_id=survivor_person_id if status == "resolved" else None,
                resolution_kind=resolution_kind,
                effective_at=effective_at,
                cause_key=f"{cause_prefix}:{link_key}",
                match_decision_id=(
                    str(record["match_decision_id"])
                    if record["match_decision_id"] is not None
                    else None
                ),
                review_case_id=(
                    str(record["review_case_id"]) if record["review_case_id"] is not None else None
                ),
            )
        )
    return await append_identity_link_revisions(tx, rows)


async def append_person_retirement_revisions(
    tx: AsyncManagedTransaction, *, person_id: str, cause_prefix: str, effective_at: str
) -> list[tuple[str, int, int]]:
    """Retire every current exported link resolved to a Person in the caller transaction."""
    result = await tx.run(GET_RESOLVED_IDENTITY_LINK_HEADS_FOR_PERSON, person_id=person_id)
    rows: list[IdentityLinkDesiredRevision] = []
    async for record in result:
        rows.append(
            IdentityLinkDesiredRevision(
                source_system=str(record["source_system"]),
                source_instance_id=str(record["source_instance_id"]),
                source_entity_type=str(record["source_entity_type"]),
                source_entity_id=str(record["source_entity_id"]),
                identity_policy_version=str(record["identity_policy_version"]),
                link_status="retired",
                hyperp_person_id=None,
                resolution_kind="person_retirement",
                effective_at=effective_at,
                cause_key=f"{cause_prefix}:{record['source_entity_type']}:{record['source_entity_id']}",
                match_decision_id=record.get("match_decision_id"),
                review_case_id=record.get("review_case_id"),
            )
        )
    return await append_identity_link_revisions(tx, rows)
