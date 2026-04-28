"""API key domain models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ApiKeyScope:
    """Well-known scopes for server-to-server API keys."""

    PERSONS_READ = "persons:read"
    PERSONS_WRITE = "persons:write"
    INGEST_WRITE = "ingest:write"
    ADMIN = "admin"


class ApiKey(BaseModel):
    """Represents a server-to-server API key (secret is never stored)."""

    id: str
    key_prefix: str
    name: str
    entity_key: str | None = None
    scopes: list[str]
    created_by: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    is_revoked: bool = False


class CreateApiKeyRequest(BaseModel):
    """Request body for creating a new API key."""

    name: str = Field(min_length=1, max_length=128)
    entity_key: str | None = None
    scopes: list[str] = Field(min_length=1)
    expires_in_days: int | None = Field(default=365, ge=1, le=730)


class ApiKeyCreatedResponse(BaseModel):
    """Response returned immediately after key creation — the secret is shown once only."""

    id: str
    key: str
    key_prefix: str
    name: str
    scopes: list[str]
    expires_at: datetime | None = None
