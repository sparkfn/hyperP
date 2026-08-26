"""Neo4j reads for machine identity-link synchronization."""

from __future__ import annotations

from pydantic import ValidationError

from src.graph.client import get_session
from src.graph.queries.identity_link_revisions import (
    GET_IDENTITY_LINK_COUNTER,
    LIST_IDENTITY_LINK_EVENTS,
    LIST_IDENTITY_LINK_SNAPSHOT,
)
from src.identity_link_types import IdentityLinkRevision


class IdentityLinkRevisionGapError(RuntimeError):
    """The immutable stream is unexpectedly non-contiguous."""


class Neo4jIdentityLinkRevisionRepository:
    async def current_revision_and_ready(self) -> tuple[int, bool]:
        async with get_session() as session:
            record = await (await session.run(GET_IDENTITY_LINK_COUNTER)).single()
        if record is None:
            raise RuntimeError("identity-link counter unavailable")
        return (
            int(record["current_revision"]),
            record["baseline_completed_at"] is not None
            and record["migration_completed_at"] is not None,
        )

    async def event_page(
        self, after_revision: int, through_revision: int, limit: int
    ) -> list[IdentityLinkRevision]:
        if after_revision == through_revision:
            return []
        if after_revision > through_revision:
            raise IdentityLinkRevisionGapError("stream checkpoint is ahead of its frozen bound")
        async with get_session() as session:
            result = await session.run(
                LIST_IDENTITY_LINK_EVENTS,
                after_revision=after_revision,
                through_revision=through_revision,
                limit=limit,
            )
            rows = [record["revision"] async for record in result]
        revisions = [IdentityLinkRevision.model_validate(row) for row in rows]
        if after_revision < through_revision and not revisions:
            raise IdentityLinkRevisionGapError("stream gap")
        expected = after_revision + 1
        for revision in revisions:
            if revision.global_revision != expected or revision.global_revision > through_revision:
                raise IdentityLinkRevisionGapError("stream gap")
            expected += 1
        if len(revisions) < limit and revisions[-1].global_revision != through_revision:
            raise IdentityLinkRevisionGapError("stream gap")
        return revisions

    async def snapshot_page(
        self, snapshot_revision: int, after_link_key: str, limit: int
    ) -> tuple[list[IdentityLinkRevision], str | None]:
        async with get_session() as session:
            result = await session.run(
                LIST_IDENTITY_LINK_SNAPSHOT,
                snapshot_revision=snapshot_revision,
                after_link_key=after_link_key,
                limit=limit + 1,
            )
            rows = [(record["link_key"], record["revision"]) async for record in result]
        has_more = len(rows) > limit
        page = rows[:limit]
        revisions: list[IdentityLinkRevision] = []
        for link_key, revision in page:
            if not isinstance(link_key, str) or not link_key or revision is None:
                raise RuntimeError("identity-link snapshot corruption")
            try:
                revisions.append(IdentityLinkRevision.model_validate(revision))
            except ValidationError as exc:
                raise RuntimeError("identity-link snapshot corruption") from exc
        return revisions, str(page[-1][0]) if has_more and page else None
