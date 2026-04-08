"""Admin endpoints for source systems and field-trust configuration."""

from __future__ import annotations

from fastapi import APIRouter, Request
from neo4j import AsyncManagedTransaction
from pydantic import BaseModel

from src.graph.client import get_session
from src.graph.converters import to_optional_str, to_str
from src.graph.queries import GET_FIELD_TRUST, LIST_SOURCE_SYSTEMS, UPDATE_FIELD_TRUST
from src.http_utils import envelope, http_error
from src.types import ApiResponse, FieldTrustUpdateRequest, TrustTier

router = APIRouter()


class SourceSystemInfo(BaseModel):
    source_system_id: str | None
    source_key: str
    display_name: str | None
    system_type: str | None
    is_active: bool
    field_trust: dict[str, str]
    created_at: str | None
    updated_at: str | None


class FieldTrustResponse(BaseModel):
    source_key: str
    display_name: str | None
    field_trust: dict[str, str]


@router.get("/v1/source-systems", response_model=ApiResponse[list[SourceSystemInfo]])
async def list_source_systems(request: Request) -> ApiResponse[list[SourceSystemInfo]]:
    """List configured source systems."""
    async with get_session() as session:
        result = await session.run(LIST_SOURCE_SYSTEMS)
        systems: list[SourceSystemInfo] = []
        async for record in result:
            ss = record["source_system"]
            if not isinstance(ss, dict):
                continue
            field_trust_raw = ss.get("field_trust")
            field_trust: dict[str, str] = {}
            if isinstance(field_trust_raw, dict):
                field_trust = {to_str(k): to_str(v) for k, v in field_trust_raw.items()}
            systems.append(
                SourceSystemInfo(
                    source_system_id=to_optional_str(ss.get("source_system_id")),
                    source_key=to_str(ss.get("source_key")),
                    display_name=to_optional_str(ss.get("display_name")),
                    system_type=to_optional_str(ss.get("system_type")),
                    is_active=bool(ss.get("is_active")),
                    field_trust=field_trust,
                    created_at=to_optional_str(ss.get("created_at")),
                    updated_at=to_optional_str(ss.get("updated_at")),
                )
            )
    return envelope(systems, request)


@router.get(
    "/v1/source-systems/{source_key}/field-trust",
    response_model=ApiResponse[FieldTrustResponse],
)
async def get_field_trust(source_key: str, request: Request) -> ApiResponse[FieldTrustResponse]:
    """Return field-level trust configuration for a source system."""
    async with get_session() as session:
        result = await session.run(GET_FIELD_TRUST, source_key=source_key)
        record = await result.single()
    if record is None:
        raise http_error(404, "not_found", f"Source system '{source_key}' not found.", request)

    field_trust_raw = record["field_trust"]
    field_trust: dict[str, str] = {}
    if isinstance(field_trust_raw, dict):
        field_trust = {to_str(k): to_str(v) for k, v in field_trust_raw.items()}

    return envelope(
        FieldTrustResponse(
            source_key=to_str(record["source_key"]),
            display_name=to_optional_str(record["display_name"]),
            field_trust=field_trust,
        ),
        request,
    )


@router.patch(
    "/v1/source-systems/{source_key}/field-trust",
    response_model=ApiResponse[FieldTrustResponse],
)
async def update_field_trust(
    source_key: str, body: FieldTrustUpdateRequest, request: Request
) -> ApiResponse[FieldTrustResponse]:
    """Update trust tiers for one or more fields on a source system."""
    if not body.updates:
        raise http_error(
            400, "invalid_request", "Provide at least one field trust update.", request
        )

    async with get_session(write=True) as session:
        merged = await session.execute_write(_update_trust_tx, source_key, body.updates)

    if merged is None:
        raise http_error(404, "not_found", f"Source system '{source_key}' not found.", request)

    return envelope(
        FieldTrustResponse(source_key=source_key, display_name=None, field_trust=merged), request
    )


async def _update_trust_tx(
    tx: AsyncManagedTransaction, source_key: str, updates: dict[str, TrustTier]
) -> dict[str, str] | None:
    current = await tx.run(GET_FIELD_TRUST, source_key=source_key)
    record = await current.single()
    if record is None:
        return None
    existing_raw = record["field_trust"]
    existing: dict[str, str] = {}
    if isinstance(existing_raw, dict):
        existing = {to_str(k): to_str(v) for k, v in existing_raw.items()}
    merged: dict[str, str] = {**existing, **{k: v.value for k, v in updates.items()}}
    await tx.run(UPDATE_FIELD_TRUST, source_key=source_key, field_trust=merged)
    return merged
