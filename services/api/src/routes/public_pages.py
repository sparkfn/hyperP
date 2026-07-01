"""Share-link endpoints: generate and consume time-limited public person page tokens."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import require_human_user
from src.auth.models import AuthUser
from src.config import config
from src.http_utils import envelope, http_error
from src.redis_client import get_redis
from src.repositories.deps import get_person_repo, get_sales_repo
from src.repositories.protocols.person import PersonRepository
from src.repositories.protocols.sales import SalesRepository
from src.types import (
    ApiResponse,
    ConnectionType,
    Person,
    PersonConnection,
    PersonIdentifier,
    SourceRecord,
)
from src.types_sales import SalesOrder

_LINK_KEY_PREFIX = "public_link:"
_PUBLIC_PAGE_LIMIT = 50


def _strip_public_person(person: Person) -> Person:
    """Omit customer-specific loyalty + machine-unit data from public share responses."""
    return person.model_copy(update={"loyalty": None, "machine_units": None})


def _strip_public_sales_order(order: SalesOrder) -> SalesOrder:
    """Omit per-sale loyalty activity from public share responses."""
    return order.model_copy(update={"points_used": None, "points_gained": None})


# Loyalty keys that must not leave the authenticated endpoints. They ride the
# identity raw_payload both as a structured top-level ``loyalty`` block AND
# serialized inside the ``person`` sub-payload (serialize_row copies every
# selected customer column). Public share responses must scrub both.
_LOYALTY_RAW_KEYS: tuple[str, ...] = (
    "loyalty",
    "points",
    "disable_loyalty",
    "current_spend_for_points",
    "current_sales_for_discount",
)


def _scrub_loyalty_from_raw_payload(raw: dict[str, object]) -> dict[str, object]:
    """Return a copy of ``raw`` with loyalty data removed, or ``raw`` unchanged."""
    changed = False
    out: dict[str, object] = {}
    for key, value in raw.items():
        if key == "loyalty":
            changed = True
            continue
        if key == "person" and isinstance(value, dict):
            sub = {sk: sv for sk, sv in value.items() if sk not in _LOYALTY_RAW_KEYS}
            if len(sub) != len(value):
                changed = True
                value = sub
        out[key] = value
    return out if changed else raw


def _strip_public_source_record(sr: SourceRecord) -> SourceRecord:
    """Omit the identity loyalty balance (carried in raw_payload) from public share responses."""
    raw = sr.raw_payload
    if not isinstance(raw, dict):
        return sr
    scrubbed = _scrub_loyalty_from_raw_payload(raw)
    if scrubbed is raw:
        return sr
    return sr.model_copy(update={"raw_payload": scrubbed})

# No auth — anyone with the token can access these endpoints.
public_router = APIRouter(prefix="/v1/public", tags=["Public"])

# Registered alongside persons router with require_active_user in app.py.
person_links_router = APIRouter(prefix="/v1/persons", tags=["Persons"])


class PublicLinkResponse(BaseModel):
    token: str
    expires_at: str


async def _resolve_person_id(token: str, request: Request) -> str:
    """Validate the public share token and return the associated person_id."""
    client = await get_redis()
    person_id: str | None = await client.get(f"{_LINK_KEY_PREFIX}{token}")
    if person_id is None:
        raise http_error(404, "link_not_found", "Share link not found or has expired.", request)
    return person_id


@person_links_router.post(
    "/{person_id}/public-link", response_model=ApiResponse[PublicLinkResponse]
)
async def create_public_link(
    person_id: str,
    request: Request,
    _user: AuthUser = Depends(require_human_user),
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[PublicLinkResponse]:
    """Generate a time-limited public share token for a person profile."""
    person = await repo.get_by_id(person_id)
    if person is None:
        raise http_error(404, "person_not_found", "Person not found.", request)

    token = str(uuid.uuid4())
    ttl = config.public_page_expiry_minutes * 60
    client = await get_redis()
    await client.set(f"{_LINK_KEY_PREFIX}{token}", person_id, ex=ttl)

    expires_at = datetime.fromtimestamp(time.time() + ttl, tz=UTC).isoformat()
    return envelope(PublicLinkResponse(token=token, expires_at=expires_at), request)


@public_router.get("/persons/{token}", response_model=ApiResponse[Person])
async def get_public_person(
    token: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[Person]:
    """Return a person profile if the share token is valid and unexpired."""
    person_id = await _resolve_person_id(token, request)
    person = await repo.get_by_id(person_id)
    if person is None:
        raise http_error(404, "person_not_found", "Person not found.", request)
    return envelope(_strip_public_person(person), request)


@public_router.get(
    "/persons/{token}/identifiers", response_model=ApiResponse[list[PersonIdentifier]]
)
async def get_public_person_identifiers(
    token: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonIdentifier]]:
    """Return identifiers for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    items, _ = await repo.get_identifiers(person_id, skip=0, limit=_PUBLIC_PAGE_LIMIT)
    return envelope(items, request)


@public_router.get(
    "/persons/{token}/connections", response_model=ApiResponse[list[PersonConnection]]
)
async def get_public_person_connections(
    token: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[PersonConnection]]:
    """Return connections for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    items, _ = await repo.get_connections(
        person_id, ConnectionType.ALL, None, skip=0, limit=_PUBLIC_PAGE_LIMIT
    )
    return envelope(items, request)


@public_router.get(
    "/persons/{token}/source-records", response_model=ApiResponse[list[SourceRecord]]
)
async def get_public_person_source_records(
    token: str,
    request: Request,
    repo: PersonRepository = Depends(get_person_repo),
) -> ApiResponse[list[SourceRecord]]:
    """Return source records for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    items, _ = await repo.get_source_records(person_id, skip=0, limit=_PUBLIC_PAGE_LIMIT)
    return envelope([_strip_public_source_record(sr) for sr in items], request)


@public_router.get("/persons/{token}/sales", response_model=ApiResponse[list[SalesOrder]])
async def get_public_person_sales(
    token: str,
    request: Request,
    repo: SalesRepository = Depends(get_sales_repo),
) -> ApiResponse[list[SalesOrder]]:
    """Return sales orders for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    items, _ = await repo.get_person_sales(person_id, skip=0, limit=_PUBLIC_PAGE_LIMIT)
    return envelope([_strip_public_sales_order(o) for o in items], request)
