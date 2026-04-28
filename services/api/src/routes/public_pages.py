"""Share-link endpoints: generate and consume time-limited public person page tokens."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.config import config
from src.graph.client import get_session
from src.graph.converters import GraphRecord, GraphValue
from src.graph.mappers import map_connection, map_person, map_person_identifier, map_source_record
from src.graph.mappers_sales import map_sales_order
from src.graph.queries import (
    GET_PERSON_BY_ID,
    GET_PERSON_CONNECTIONS_ALL,
    GET_PERSON_IDENTIFIERS,
    GET_PERSON_SALES,
    GET_PERSON_SOURCE_RECORDS,
)
from src.http_utils import envelope, http_error
from src.redis_client import get_redis
from src.types import ApiResponse, Person, PersonConnection, PersonIdentifier, SourceRecord
from src.types_sales import SalesOrder

_LINK_KEY_PREFIX = "public_link:"
_PUBLIC_PAGE_LIMIT = 50

# No auth — anyone with the token can access these endpoints.
public_router = APIRouter(prefix="/v1/public")

# Registered alongside persons router with require_active_user in app.py.
person_links_router = APIRouter(prefix="/v1/persons")


class PublicLinkResponse(BaseModel):
    token: str
    expires_at: str


def _record_to_dict(keys: list[str], values: list[GraphValue]) -> GraphRecord:
    return dict(zip(keys, values, strict=True))


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
async def create_public_link(person_id: str, request: Request) -> ApiResponse[PublicLinkResponse]:
    """Generate a time-limited public share token for a person profile."""
    async with get_session() as session:
        result = await session.run(GET_PERSON_BY_ID, person_id=person_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "person_not_found", "Person not found.", request)

    token = str(uuid.uuid4())
    ttl = config.public_page_expiry_minutes * 60
    client = await get_redis()
    await client.set(f"{_LINK_KEY_PREFIX}{token}", person_id, ex=ttl)

    expires_at = datetime.fromtimestamp(time.time() + ttl, tz=UTC).isoformat()
    return envelope(PublicLinkResponse(token=token, expires_at=expires_at), request)


@public_router.get("/persons/{token}", response_model=ApiResponse[Person])
async def get_public_person(token: str, request: Request) -> ApiResponse[Person]:
    """Return a person profile if the share token is valid and unexpired."""
    person_id = await _resolve_person_id(token, request)

    async with get_session() as session:
        result = await session.run(GET_PERSON_BY_ID, person_id=person_id)
        record = await result.single()
    if record is None:
        raise http_error(404, "person_not_found", "Person not found.", request)

    return envelope(
        map_person(_record_to_dict(record.keys(), list(record.values()))),
        request,
    )


@public_router.get(
    "/persons/{token}/identifiers", response_model=ApiResponse[list[PersonIdentifier]]
)
async def get_public_person_identifiers(
    token: str, request: Request
) -> ApiResponse[list[PersonIdentifier]]:
    """Return identifiers for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_IDENTIFIERS, person_id=person_id, skip=0, limit=_PUBLIC_PAGE_LIMIT
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    return envelope([map_person_identifier(r) for r in records], request)


@public_router.get(
    "/persons/{token}/connections", response_model=ApiResponse[list[PersonConnection]]
)
async def get_public_person_connections(
    token: str, request: Request
) -> ApiResponse[list[PersonConnection]]:
    """Return connections for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_CONNECTIONS_ALL,
            person_id=person_id,
            identifier_type=None,
            skip=0,
            limit=_PUBLIC_PAGE_LIMIT,
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    return envelope([map_connection(r) for r in records], request)


@public_router.get(
    "/persons/{token}/source-records", response_model=ApiResponse[list[SourceRecord]]
)
async def get_public_person_source_records(
    token: str, request: Request
) -> ApiResponse[list[SourceRecord]]:
    """Return source records for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_SOURCE_RECORDS, person_id=person_id, skip=0, limit=_PUBLIC_PAGE_LIMIT
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    return envelope([map_source_record(r) for r in records], request)


@public_router.get("/persons/{token}/sales", response_model=ApiResponse[list[SalesOrder]])
async def get_public_person_sales(token: str, request: Request) -> ApiResponse[list[SalesOrder]]:
    """Return sales orders for the person referenced by the share token."""
    person_id = await _resolve_person_id(token, request)
    async with get_session() as session:
        result = await session.run(
            GET_PERSON_SALES, person_id=person_id, skip=0, limit=_PUBLIC_PAGE_LIMIT
        )
        records = [_record_to_dict(r.keys(), list(r.values())) async for r in result]
    return envelope([map_sales_order(r) for r in records], request)
