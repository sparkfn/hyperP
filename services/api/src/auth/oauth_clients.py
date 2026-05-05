"""OAuth client credential generation, storage, and validation."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from src.auth.models import AuthUser
from src.auth.oauth_client_models import (
    ALLOWED_OAUTH_CLIENT_SCOPES,
    CreateOAuthClientRequest,
    CreateOAuthClientSecretRequest,
    OAuthClient,
    OAuthClientCreatedResponse,
    OAuthClientSecret,
    OAuthClientSecretCreatedResponse,
)
from src.config import config
from src.graph.client import get_session
from src.graph.converters import GraphValue, to_datetime, to_optional_str, to_str
from src.graph.queries.oauth_clients import (
    CREATE_OAUTH_CLIENT_ID_CONSTRAINT,
    CREATE_OAUTH_CLIENT_SECRET,
    CREATE_OAUTH_CLIENT_WITH_SECRET,
    CREATE_OAUTH_SECRET_ID_CONSTRAINT,
    DELETE_OAUTH_CLIENT,
    DISABLE_OAUTH_CLIENT,
    GET_OAUTH_CLIENT_BY_ID,
    GET_OAUTH_CLIENT_FOR_VALIDATION,
    GET_OAUTH_CLIENTS_FOR_ADMIN,
    REVOKE_OAUTH_CLIENT_SECRET,
    UPDATE_OAUTH_CLIENT_LAST_USED,
    UPDATE_OAUTH_SECRET_LAST_USED,
)


def generate_client_id() -> str:
    """Generate a public OAuth client id."""
    return f"hpc_{secrets.token_urlsafe(24)}"


def generate_client_secret() -> str:
    """Generate a one-time OAuth client secret."""
    return f"hps_{secrets.token_urlsafe(32)}"


def _hash_key(explicit_hash_key: str | None = None) -> bytes:
    key = explicit_hash_key or config.oauth_secret_hash_key
    if not key:
        raise ValueError("OAuth client secret hash key is not configured.")
    return key.encode()


def hash_client_secret(secret: str, *, hash_key: str | None = None) -> str:
    """HMAC-SHA256 hash of a client secret."""
    return hmac.new(_hash_key(hash_key), secret.encode(), hashlib.sha256).hexdigest()


def verify_client_secret(secret: str, digest: str, *, hash_key: str | None = None) -> bool:
    """Return whether a plain secret matches a stored digest."""
    candidate = hash_client_secret(secret, hash_key=hash_key)
    return hmac.compare_digest(candidate, digest)


def check_scope(scopes: list[str], required: str) -> bool:
    """Return True if scopes include required or admin."""
    return "admin" in scopes or required in scopes


def requested_scopes_or_default(requested: str | None, assigned: list[str]) -> list[str] | None:
    """Return requested scopes when they are supported and assigned."""
    if requested is None or not requested.strip():
        return assigned
    raw_scopes = requested.split(" ")
    if any(not scope.strip() for scope in raw_scopes):
        return None
    requested_scopes = [scope.strip() for scope in raw_scopes]
    requested_set = set(requested_scopes)
    if len(requested_set) != len(requested_scopes):
        return None
    if any(scope not in ALLOWED_OAUTH_CLIENT_SCOPES for scope in requested_scopes):
        return None
    assigned_set = set(assigned)
    if "admin" not in assigned_set and any(
        scope not in assigned_set for scope in requested_scopes
    ):
        return None
    return requested_scopes


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)




def is_secret_usable(secret: OAuthClientSecret, *, now: datetime | None = None) -> bool:
    """Return whether a secret is active, non-revoked, and non-expired."""
    current = _as_naive_utc(now or datetime.now(UTC))
    if secret.revoked_at is not None:
        return False
    if secret.expires_at is None:
        return True
    expires_at = _as_naive_utc(secret.expires_at)
    return current <= expires_at


def _scopes_from_record(value: GraphValue) -> list[str]:
    if value is None:
        return []
    return [scope for scope in to_str(value).split(",") if scope]


def _secret_from_record(record: Mapping[str, GraphValue]) -> OAuthClientSecret:
    return OAuthClientSecret(
        secret_id=to_str(record.get("secret_id")),
        secret_prefix=to_str(record.get("secret_prefix")),
        created_at=to_datetime(record.get("created_at")),
        expires_at=to_datetime(record.get("expires_at")),
        revoked_at=to_datetime(record.get("revoked_at")),
        last_used_at=to_datetime(record.get("last_used_at")),
    )


def _client_from_record(record: Mapping[str, GraphValue]) -> OAuthClient:
    secrets_raw = record.get("secrets", [])
    secrets: list[OAuthClientSecret] = []
    if isinstance(secrets_raw, list):
        for item in secrets_raw:
            if isinstance(item, dict) and item.get("secret_id") is not None:
                secrets.append(_secret_from_record(item))
    return OAuthClient(
        client_id=to_str(record.get("client_id")),
        name=to_str(record.get("name")),
        entity_key=to_optional_str(record.get("entity_key")),
        scopes=_scopes_from_record(record.get("scopes")),
        created_by=to_str(record.get("created_by")),
        created_at=to_datetime(record.get("created_at")),
        disabled_at=to_datetime(record.get("disabled_at")),
        last_used_at=to_datetime(record.get("last_used_at")),
        secrets=secrets,
    )


def _record_mapping(value: GraphValue | None) -> Mapping[str, GraphValue] | None:
    if isinstance(value, dict):
        return value
    return None


async def ensure_oauth_client_constraints() -> None:
    """Create OAuth client constraints if they do not exist."""
    async with get_session(write=True) as session:
        await session.run(CREATE_OAUTH_CLIENT_ID_CONSTRAINT)
        await session.run(CREATE_OAUTH_SECRET_ID_CONSTRAINT)


async def get_oauth_client_by_id(client_id: str) -> OAuthClient | None:
    """Return one OAuth client by id, including audit metadata and secrets."""
    async with get_session() as session:
        result = await session.run(GET_OAUTH_CLIENT_BY_ID, client_id=client_id)
        record = await result.single()
        if record is None:
            return None
        client = _record_mapping(record.get("client"))
        if client is None:
            return None
        return _client_from_record(client)


async def create_oauth_client(
    req: CreateOAuthClientRequest, actor: AuthUser
) -> OAuthClientCreatedResponse:
    """Create an OAuth client and its first secret."""
    client_id = generate_client_id()
    client_secret = generate_client_secret()
    secret_id = f"sec_{uuid.uuid4()}"
    secret_prefix = client_secret[:10]
    secret_hash = hash_client_secret(client_secret)
    now = datetime.now(UTC).replace(tzinfo=None)
    expires_at = None
    if req.secret_expires_in_days is not None:
        expires_at = now + timedelta(days=req.secret_expires_in_days)

    async with get_session(write=True) as session:
        await session.run(
            CREATE_OAUTH_CLIENT_WITH_SECRET,
            client_id=client_id,
            name=req.name,
            entity_key=req.entity_key,
            scopes=",".join(req.scopes),
            created_by=actor.email,
            created_at=now.isoformat(),
            secret_id=secret_id,
            secret_hash=secret_hash,
            secret_prefix=secret_prefix,
            secret_created_at=now.isoformat(),
            secret_expires_at=expires_at.isoformat() if expires_at else None,
        )

    return OAuthClientCreatedResponse(
        client_id=client_id,
        client_secret=client_secret,
        secret_id=secret_id,
        secret_prefix=secret_prefix,
        name=req.name,
        scopes=req.scopes,
        secret_expires_at=expires_at,
    )


async def list_oauth_clients() -> list[OAuthClient]:
    """Return OAuth clients for the admin UI."""
    async with get_session() as session:
        result = await session.run(GET_OAUTH_CLIENTS_FOR_ADMIN)
        clients: list[OAuthClient] = []
        async for record in result:
            client = _record_mapping(record.get("client"))
            if client is not None:
                clients.append(_client_from_record(client))
        return clients


async def create_oauth_client_secret(
    client_id: str, req: CreateOAuthClientSecretRequest
) -> OAuthClientSecretCreatedResponse | None:
    """Create an additional secret for an OAuth client."""
    client_secret = generate_client_secret()
    secret_id = f"sec_{uuid.uuid4()}"
    secret_prefix = client_secret[:10]
    secret_hash = hash_client_secret(client_secret)
    now = datetime.now(UTC).replace(tzinfo=None)
    expires_at = None
    if req.expires_in_days is not None:
        expires_at = now + timedelta(days=req.expires_in_days)

    async with get_session(write=True) as session:
        result = await session.run(
            CREATE_OAUTH_CLIENT_SECRET,
            client_id=client_id,
            secret_id=secret_id,
            secret_hash=secret_hash,
            secret_prefix=secret_prefix,
            created_at=now.isoformat(),
            expires_at=expires_at.isoformat() if expires_at else None,
        )
        if await result.single() is None:
            return None

    return OAuthClientSecretCreatedResponse(
        client_id=client_id,
        client_secret=client_secret,
        secret_id=secret_id,
        secret_prefix=secret_prefix,
        expires_at=expires_at,
    )


async def revoke_oauth_client_secret(client_id: str, secret_id: str) -> bool:
    """Revoke one OAuth client secret."""
    async with get_session(write=True) as session:
        result = await session.run(
            REVOKE_OAUTH_CLIENT_SECRET, client_id=client_id, secret_id=secret_id
        )
        return await result.single() is not None


async def disable_oauth_client(client_id: str) -> bool:
    """Disable an OAuth client without deleting its audit trail."""
    async with get_session(write=True) as session:
        result = await session.run(DISABLE_OAUTH_CLIENT, client_id=client_id)
        return await result.single() is not None


async def delete_oauth_client(client_id: str) -> bool:
    """Delete an OAuth client and its secrets."""
    async with get_session(write=True) as session:
        result = await session.run(GET_OAUTH_CLIENT_BY_ID, client_id=client_id)
        if await result.single() is None:
            return False
        await session.run(DELETE_OAUTH_CLIENT, client_id=client_id)
    return True


async def validate_client_credentials(
    client_id: str, client_secret: str
) -> tuple[OAuthClient, list[str]] | None:
    """Validate OAuth client credentials and return client plus assigned scopes."""
    async with get_session() as session:
        result = await session.run(GET_OAUTH_CLIENT_FOR_VALIDATION, client_id=client_id)
        async for record in result:
            raw = _record_mapping(record.get("client"))
            if raw is None or raw.get("disabled_at") is not None:
                continue
            secret_raw = _record_mapping(raw.get("secret"))
            if secret_raw is None:
                continue
            secret_hash = to_str(secret_raw.get("secret_hash"))
            if not verify_client_secret(client_secret, secret_hash):
                continue
            secret = _secret_from_record(secret_raw)
            if not is_secret_usable(secret):
                return None
            client = _client_from_record({**raw, "secrets": [dict(secret_raw)]})
            _touch_last_used(client.client_id, secret.secret_id)
            return client, client.scopes
    return None


def _touch_last_used(client_id: str, secret_id: str) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_update_last_used(client_id, secret_id))
    except RuntimeError:
        pass


async def _update_last_used(client_id: str, secret_id: str) -> None:
    try:
        async with get_session(write=True) as session:
            await session.run(UPDATE_OAUTH_CLIENT_LAST_USED, client_id=client_id)
            await session.run(
                UPDATE_OAUTH_SECRET_LAST_USED, client_id=client_id, secret_id=secret_id
            )
    except Exception:  # noqa: BLE001
        pass
