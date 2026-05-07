"""OAuth client-credentials domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from src.auth.models import AuthUser

OAuthGrantType = Literal["client_credentials"]


class OAuthClientScope:
    """Well-known scopes for machine OAuth clients."""

    PERSONS_READ = "persons:read"
    PERSONS_WRITE = "persons:write"
    INGEST_WRITE = "ingest:write"
    ADMIN = "admin"


ALLOWED_OAUTH_CLIENT_SCOPES = frozenset(
    {
        OAuthClientScope.PERSONS_READ,
        OAuthClientScope.PERSONS_WRITE,
        OAuthClientScope.INGEST_WRITE,
        OAuthClientScope.ADMIN,
    }
)


def validate_oauth_client_scopes(scopes: list[str]) -> list[str]:
    """Validate OAuth client scopes against supported machine scopes."""
    seen: set[str] = set()
    for scope in scopes:
        if not scope.strip():
            raise ValueError("Scopes must not be blank.")
        if scope not in ALLOWED_OAUTH_CLIENT_SCOPES:
            raise ValueError(f"Unknown OAuth client scope: {scope}.")
        if scope in seen:
            raise ValueError(f"Duplicate OAuth client scope: {scope}.")
        seen.add(scope)
    return scopes


class OAuthClientUser(AuthUser):
    """AuthUser subclass for OAuth-client-authenticated callers."""

    source: str = "oauth_client"
    client_id: str
    key_scopes: list[str] = Field(default_factory=list)


class OAuthClientSecret(BaseModel):
    """Stored metadata for one OAuth client secret; plain secret is never stored."""

    secret_id: str
    secret_prefix: str
    created_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class OAuthClient(BaseModel):
    """Machine OAuth client metadata."""

    client_id: str
    name: str
    entity_key: str | None = None
    scopes: list[str]
    created_by: str
    created_at: datetime | None = None
    disabled_at: datetime | None = None
    last_used_at: datetime | None = None
    secrets: list[OAuthClientSecret] = Field(default_factory=list)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str]) -> list[str]:
        """Validate supported, unique, non-blank scopes."""
        return validate_oauth_client_scopes(scopes)


class CreateOAuthClientRequest(BaseModel):
    """Request body for creating an OAuth client and its first secret."""

    name: str = Field(min_length=1, max_length=128)
    entity_key: str | None = None
    scopes: list[str] = Field(min_length=1)
    secret_expires_in_days: int | None = Field(default=365, ge=1, le=730)

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: list[str]) -> list[str]:
        """Validate supported, unique, non-blank scopes."""
        return validate_oauth_client_scopes(scopes)


class OAuthClientCreatedResponse(BaseModel):
    """OAuth client creation response; plain secret is shown once only."""

    client_id: str
    client_secret: str
    secret_id: str
    secret_prefix: str
    name: str
    scopes: list[str]
    secret_expires_at: datetime | None = None


class CreateOAuthClientSecretRequest(BaseModel):
    """Request body for rotating an OAuth client secret."""

    expires_in_days: int | None = Field(default=365, ge=1, le=730)


class OAuthClientSecretCreatedResponse(BaseModel):
    """Secret rotation response; plain secret is shown once only."""

    client_id: str
    client_secret: str
    secret_id: str
    secret_prefix: str
    expires_at: datetime | None = None


class OAuthTokenRequest(BaseModel):
    """Parsed OAuth token request."""

    grant_type: OAuthGrantType
    client_id: str
    client_secret: str
    scope: str | None = None


class OAuthTokenResponse(BaseModel):
    """OAuth access token response."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    scope: str


class OAuthErrorResponse(BaseModel):
    """Standards-compatible OAuth error response."""

    error: str
    error_description: str
