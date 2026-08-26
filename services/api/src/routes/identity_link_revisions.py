"""OAuth-only identity-link event and fixed snapshot endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from src.auth.deps import require_unscoped_oauth_client_scope
from src.auth.oauth_client_models import OAuthClientScope
from src.http_utils import http_error, request_id
from src.identity_link_cursors import (
    IdentityLinkCursor,
    decode_identity_link_cursor,
    encode_identity_link_cursor,
)
from src.identity_link_types import IdentityLinkRevision
from src.repositories.deps import get_identity_link_revision_repo
from src.repositories.neo4j.identity_link_revision import IdentityLinkRevisionGapError
from src.repositories.protocols.identity_link_revision import IdentityLinkRevisionRepository
from src.types import ResponseMeta

router = APIRouter(tags=["Identity links"])


class IdentityLinkEventsResponse(BaseModel):
    """Frozen event window page with the consumer's durable upper bound."""

    data: list[IdentityLinkRevision]
    meta: ResponseMeta
    through_revision: int = Field(ge=0)


class IdentityLinkSnapshotResponse(BaseModel):
    """Fixed snapshot page with its durable recovery boundary."""

    data: list[IdentityLinkRevision]
    meta: ResponseMeta
    snapshot_revision: int = Field(ge=0)


def _limit(raw: int | None) -> int:
    if raw is None:
        return 50
    if isinstance(raw, bool) or raw < 1 or raw > 200:
        raise ValueError("limit must be between 1 and 200")
    return raw


@router.get(
    "/identity-links/events",
    operation_id="list_identity_link_events_machine",
    response_model=IdentityLinkEventsResponse,
    dependencies=[
        Depends(require_unscoped_oauth_client_scope(OAuthClientScope.IDENTITY_LINKS_READ))
    ],
)
async def list_identity_link_events(
    request: Request,
    after_revision: int | None = Query(default=None, ge=0),
    cursor: str | None = None,
    limit: int | None = None,
    repo: IdentityLinkRevisionRepository = Depends(get_identity_link_revision_repo),
) -> IdentityLinkEventsResponse:
    try:
        page_limit = _limit(limit)
    except ValueError as exc:
        raise http_error(400, "invalid_request", str(exc), request) from exc
    if cursor and after_revision is not None:
        raise http_error(
            400, "invalid_request", "cursor and after_revision cannot be combined.", request
        )
    try:
        if cursor:
            state = decode_identity_link_cursor(cursor, "events")
            assert state.after_revision is not None and state.through_revision is not None
            after, through = state.after_revision, state.through_revision
        else:
            after = after_revision or 0
            through, _ = await repo.current_revision_and_ready()
        items = await repo.event_page(after, through, page_limit)
    except ValueError as exc:
        raise http_error(400, "invalid_cursor", "Invalid identity-link cursor.", request) from exc
    except IdentityLinkRevisionGapError as exc:
        raise http_error(
            503, "identity_link_revision_gap", "Identity-link stream has a revision gap.", request
        ) from exc
    next_value = (
        items[-1].global_revision
        if len(items) == page_limit and items[-1].global_revision < through
        else None
    )
    next_cursor = (
        encode_identity_link_cursor(
            IdentityLinkCursor(kind="events", after_revision=next_value, through_revision=through)
        )
        if next_value is not None
        else None
    )
    return IdentityLinkEventsResponse(
        data=items,
        meta=ResponseMeta(request_id=request_id(request), next_cursor=next_cursor),
        through_revision=through,
    )


@router.get(
    "/identity-links/snapshot",
    operation_id="list_identity_link_snapshot_machine",
    response_model=IdentityLinkSnapshotResponse,
    dependencies=[
        Depends(require_unscoped_oauth_client_scope(OAuthClientScope.IDENTITY_LINKS_READ))
    ],
)
async def list_identity_link_snapshot(
    request: Request,
    cursor: str | None = None,
    limit: int | None = None,
    repo: IdentityLinkRevisionRepository = Depends(get_identity_link_revision_repo),
) -> IdentityLinkSnapshotResponse:
    try:
        page_limit = _limit(limit)
    except ValueError as exc:
        raise http_error(400, "invalid_request", str(exc), request) from exc
    try:
        if cursor:
            state = decode_identity_link_cursor(cursor, "snapshot")
            assert state.snapshot_revision is not None and state.after_link_key is not None
            revision, after_key = state.snapshot_revision, state.after_link_key
        else:
            revision, ready = await repo.current_revision_and_ready()
            if not ready:
                raise RuntimeError("not ready")
            after_key = ""
        items, next_key = await repo.snapshot_page(revision, after_key, page_limit)
    except ValueError as exc:
        raise http_error(400, "invalid_cursor", "Invalid identity-link cursor.", request) from exc
    except RuntimeError as exc:
        if str(exc) == "not ready":
            raise http_error(
                503,
                "identity_link_snapshot_not_ready",
                "Identity-link baseline is not ready.",
                request,
            ) from exc
        raise http_error(
            503, "identity_link_stream_unavailable", "Identity-link stream is unavailable.", request
        ) from exc
    next_cursor = (
        encode_identity_link_cursor(
            IdentityLinkCursor(kind="snapshot", snapshot_revision=revision, after_link_key=next_key)
        )
        if next_key
        else None
    )
    return IdentityLinkSnapshotResponse(
        data=items,
        meta=ResponseMeta(request_id=request_id(request), next_cursor=next_cursor),
        snapshot_revision=revision,
    )
