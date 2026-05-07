"""Tests for OAuth client credential helpers and service behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import TracebackType
from unittest.mock import patch

import pytest
from src.auth import oauth_clients
from src.auth.models import AuthUser
from src.auth.oauth_client_models import (
    CreateOAuthClientRequest,
    CreateOAuthClientSecretRequest,
    OAuthClient,
    OAuthClientCreatedResponse,
    OAuthClientSecret,
    OAuthClientSecretCreatedResponse,
)
from src.auth.oauth_clients import (
    check_scope,
    create_oauth_client,
    create_oauth_client_secret,
    disable_oauth_client,
    ensure_oauth_client_constraints,
    generate_client_id,
    generate_client_secret,
    hash_client_secret,
    is_secret_usable,
    requested_scopes_or_default,
    validate_client_credentials,
    verify_client_secret,
)
from src.graph.converters import GraphValue
from src.graph.queries.oauth_clients import (
    GET_OAUTH_CLIENT_BY_ID,
    GET_OAUTH_CLIENTS_FOR_ADMIN,
)

TEST_HASH_KEY = "test-oauth-secret-hash-key"


def test_generate_client_id_has_expected_prefix() -> None:
    client_id = generate_client_id()

    assert client_id.startswith("hpc_")
    assert len(client_id) > 20


def test_generated_secret_is_returned_once_and_hash_verifies() -> None:
    secret = generate_client_secret()
    digest = hash_client_secret(secret, hash_key="test-key")

    assert secret.startswith("hps_")
    assert digest != secret
    assert verify_client_secret(secret, digest, hash_key="test-key")
    assert not verify_client_secret("hps_wrong", digest, hash_key="test-key")


def test_hash_client_secret_requires_dedicated_configured_hash_key() -> None:
    with (
        patch("src.auth.oauth_clients.config.oauth_secret_hash_key", ""),
        patch("src.auth.oauth_clients.config.oauth_private_key_pem", "private-key-fallback"),
        pytest.raises(ValueError),
    ):
        hash_client_secret("hps_secret")


def test_check_scope_treats_admin_as_superset() -> None:
    assert check_scope(["admin"], "persons:read")
    assert check_scope(["persons:read"], "persons:read")
    assert not check_scope(["persons:read"], "persons:write")


def test_requested_scopes_default_to_assigned_scopes() -> None:
    assert requested_scopes_or_default(None, ["persons:read", "ingest:write"]) == [
        "persons:read",
        "ingest:write",
    ]


def test_requested_scopes_must_be_subset() -> None:
    assert requested_scopes_or_default("persons:read", ["persons:read"]) == [
        "persons:read"
    ]
    assert requested_scopes_or_default("persons:write", ["persons:read"]) is None


def test_requested_scopes_allow_admin_superset() -> None:
    assert requested_scopes_or_default("persons:read", ["admin"]) == ["persons:read"]


def test_requested_scopes_rejects_unknown_scope_for_admin_client() -> None:
    assert requested_scopes_or_default("unknown:scope", ["admin"]) is None


def test_requested_scopes_rejects_duplicate_scope_for_admin_client() -> None:
    assert requested_scopes_or_default("persons:read persons:read", ["admin"]) is None


def test_secret_usable_requires_not_revoked_and_not_expired() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    usable = OAuthClientSecret(
        secret_id="sec_1",
        secret_prefix="hps_abc",
        created_at=now,
        expires_at=now + timedelta(days=1),
        revoked_at=None,
        last_used_at=None,
    )
    expired = usable.model_copy(update={"expires_at": now - timedelta(seconds=1)})
    revoked = usable.model_copy(update={"revoked_at": now})

    assert is_secret_usable(usable, now=now)
    assert not is_secret_usable(expired, now=now)
    assert not is_secret_usable(revoked, now=now)


def test_secret_usable_accepts_timezone_aware_now_with_naive_and_aware_expiry() -> None:
    aware_now = datetime.now(UTC)
    naive_expiry = aware_now.replace(tzinfo=None) + timedelta(days=1)
    aware_expiry = aware_now + timedelta(days=1)
    naive_secret = OAuthClientSecret(
        secret_id="sec_naive",
        secret_prefix="hps_naive",
        created_at=aware_now.replace(tzinfo=None),
        expires_at=naive_expiry,
        revoked_at=None,
        last_used_at=None,
    )
    aware_secret = naive_secret.model_copy(
        update={"secret_id": "sec_aware", "expires_at": aware_expiry}
    )

    assert is_secret_usable(naive_secret, now=aware_now)
    assert is_secret_usable(aware_secret, now=aware_now)


def test_create_oauth_client_request_rejects_blank_duplicate_and_unknown_scopes() -> None:
    with pytest.raises(ValueError):
        CreateOAuthClientRequest(name="POS sync", scopes=[""])
    with pytest.raises(ValueError):
        CreateOAuthClientRequest(name="POS sync", scopes=["persons:read", "persons:read"])
    with pytest.raises(ValueError):
        CreateOAuthClientRequest(name="POS sync", scopes=["persons:delete"])


def test_oauth_client_model_supports_admin_actor_shape() -> None:
    actor = AuthUser(
        email="admin@example.com",
        google_sub="sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
        first_time=False,
    )
    client = OAuthClient(
        client_id="hpc_123",
        name="POS sync",
        entity_key="fundbox",
        scopes=["persons:read"],
        created_by=actor.email,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[],
    )

    assert client.created_by == "admin@example.com"
    assert client.entity_key == "fundbox"


class _FakeResult:
    def __init__(self, records: list[dict[str, GraphValue]]) -> None:
        self._records = records

    async def single(self) -> dict[str, GraphValue] | None:
        return self._records[0] if self._records else None

    def __aiter__(self) -> AsyncIterator[dict[str, GraphValue]]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[dict[str, GraphValue]]:
        for record in self._records:
            yield record


class _FakeSession:
    def __init__(self, result: _FakeResult | None = None) -> None:
        self.result = result or _FakeResult([])
        self.calls: list[tuple[str, dict[str, GraphValue]]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(self, query: str, **params: GraphValue) -> _FakeResult:
        self.calls.append((query, params))
        return self.result


class _FakeSessionFactory:
    def __init__(self, *sessions: _FakeSession) -> None:
        self._sessions = list(sessions)
        self.sessions: list[_FakeSession] = []
        self.write_flags: list[bool] = []

    def __call__(self, *, write: bool = False) -> _FakeSession:
        self.write_flags.append(write)
        if self._sessions:
            session = self._sessions.pop(0)
        else:
            session = _FakeSession()
        self.sessions.append(session)
        return session


class _TouchRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, client_id: str, secret_id: str) -> None:
        self.calls.append((client_id, secret_id))


def _ignore_touch_last_used(client_id: str, secret_id: str) -> None:
    _ = (client_id, secret_id)


def _validation_client_record(
    *,
    client_secret: str,
    disabled_at: datetime | None = None,
    revoked_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict[str, GraphValue]:
    now = datetime.now(UTC).replace(tzinfo=None)
    return {
        "client": {
            "client_id": "hpc_valid",
            "name": "POS sync",
            "entity_key": "fundbox",
            "scopes": "persons:read,ingest:write",
            "created_by": "admin@example.com",
            "created_at": now,
            "disabled_at": disabled_at,
            "last_used_at": None,
            "secret": {
                "secret_id": "sec_valid",
                "secret_prefix": client_secret[:10],
                "secret_hash": hash_client_secret(client_secret, hash_key=TEST_HASH_KEY),
                "created_at": now,
                "expires_at": expires_at or now + timedelta(days=1),
                "revoked_at": revoked_at,
                "last_used_at": None,
            },
        }
    }


def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
        first_time=False,
    )


def test_admin_oauth_clients_query_orders_before_projection() -> None:
    query = GET_OAUTH_CLIENTS_FOR_ADMIN

    assert "WITH c, secrets ORDER BY c.created_at DESC" in query
    assert "ORDER BY client.created_at DESC" not in query
    assert "ORDER BY c.created_at DESC" not in query.rsplit("RETURN", maxsplit=1)[-1]


@pytest.mark.parametrize(
    "query",
    [GET_OAUTH_CLIENTS_FOR_ADMIN, GET_OAUTH_CLIENT_BY_ID],
)
def test_oauth_client_queries_aggregate_secrets_before_projection(query: str) -> None:
    assert "WITH c, collect(s {" in query
    assert "secrets: secrets" in query
    assert "secrets: collect(s {" not in query


@pytest.mark.asyncio
async def test_create_oauth_client_stores_client_and_first_secret() -> None:
    session = _FakeSession()
    req = CreateOAuthClientRequest(
        name="POS sync",
        entity_key="fundbox",
        scopes=["persons:read"],
        secret_expires_in_days=30,
    )

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        created = await create_oauth_client(req, _admin_user())

    assert isinstance(created, OAuthClientCreatedResponse)
    assert created.client_id.startswith("hpc_")
    assert created.client_secret.startswith("hps_")
    assert created.secret_id.startswith("sec_")
    assert session.calls[0][1]["name"] == "POS sync"
    assert session.calls[0][1]["entity_key"] == "fundbox"
    assert session.calls[0][1]["scopes"] == "persons:read"
    assert session.calls[0][1]["secret_hash"] != created.client_secret


@pytest.mark.asyncio
async def test_create_oauth_client_secret_returns_one_time_secret() -> None:
    session = _FakeSession(_FakeResult([{"created": True}]))
    req = CreateOAuthClientSecretRequest(expires_in_days=60)

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        created = await create_oauth_client_secret("hpc_123", req)

    assert isinstance(created, OAuthClientSecretCreatedResponse)
    assert created.client_id == "hpc_123"
    assert created.client_secret.startswith("hps_")
    assert session.calls[0][1]["client_id"] == "hpc_123"
    assert session.calls[0][1]["secret_hash"] != created.client_secret


@pytest.mark.asyncio
async def test_create_oauth_client_secret_returns_none_when_client_missing_or_disabled() -> None:
    session = _FakeSession(_FakeResult([]))
    req = CreateOAuthClientSecretRequest(expires_in_days=60)

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        created = await create_oauth_client_secret("hpc_missing", req)

    assert created is None
    assert session.calls[0][1]["client_id"] == "hpc_missing"


@pytest.mark.asyncio
async def test_get_oauth_client_by_id_returns_client() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    session = _FakeSession(
        _FakeResult(
            [
                {
                    "client": {
                        "client_id": "hpc_valid",
                        "name": "POS sync",
                        "entity_key": "fundbox",
                        "scopes": "persons:read,ingest:write",
                        "created_by": "admin@example.com",
                        "created_at": now,
                        "disabled_at": None,
                        "last_used_at": None,
                        "secrets": [],
                    }
                }
            ]
        )
    )

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        client = await oauth_clients.get_oauth_client_by_id("hpc_valid")

    assert client is not None
    assert client.client_id == "hpc_valid"
    assert client.entity_key == "fundbox"
    assert client.scopes == ["persons:read", "ingest:write"]


@pytest.mark.asyncio
async def test_get_oauth_client_by_id_returns_none_for_missing_client() -> None:
    session = _FakeSession(_FakeResult([]))

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        assert await oauth_clients.get_oauth_client_by_id("hpc_missing") is None


@pytest.mark.asyncio
async def test_ensure_oauth_client_constraints_propagates_failures() -> None:
    class _FailingSession(_FakeSession):
        async def run(self, query: str, **params: GraphValue) -> _FakeResult:
            _ = (query, params)
            raise RuntimeError("neo4j unavailable")

    with patch("src.auth.oauth_clients.get_session", return_value=_FailingSession()):
        with pytest.raises(RuntimeError, match="neo4j unavailable"):
            await ensure_oauth_client_constraints()


@pytest.mark.asyncio
async def test_validate_client_credentials_accepts_active_client_and_touches_last_used() -> None:
    client_secret = "hps_valid_secret"
    validation_session = _FakeSession(
        _FakeResult([_validation_client_record(client_secret=client_secret)])
    )
    factory = _FakeSessionFactory(validation_session)
    touch_recorder = _TouchRecorder()

    with (
        patch("src.auth.oauth_clients.config.oauth_secret_hash_key", TEST_HASH_KEY),
        patch("src.auth.oauth_clients.get_session", factory),
        patch("src.auth.oauth_clients._touch_last_used", touch_recorder),
    ):
        result = await validate_client_credentials("hpc_valid", client_secret)

    assert result is not None
    client, scopes = result
    assert client.client_id == "hpc_valid"
    assert client.entity_key == "fundbox"
    assert scopes == ["persons:read", "ingest:write"]
    assert touch_recorder.calls == [("hpc_valid", "sec_valid")]


@pytest.mark.asyncio
async def test_validate_client_credentials_rejects_disabled_client() -> None:
    client_secret = "hps_valid_secret"
    validation_session = _FakeSession(
        _FakeResult(
            [
                _validation_client_record(
                    client_secret=client_secret,
                    disabled_at=datetime.now(UTC).replace(tzinfo=None),
                )
            ]
        )
    )

    with (
        patch("src.auth.oauth_clients.config.oauth_secret_hash_key", TEST_HASH_KEY),
        patch("src.auth.oauth_clients.get_session", return_value=validation_session),
        patch("src.auth.oauth_clients._touch_last_used", _ignore_touch_last_used),
    ):
        assert await validate_client_credentials("hpc_valid", client_secret) is None


@pytest.mark.asyncio
async def test_validate_client_credentials_rejects_revoked_secret() -> None:
    client_secret = "hps_valid_secret"
    validation_session = _FakeSession(
        _FakeResult(
            [
                _validation_client_record(
                    client_secret=client_secret,
                    revoked_at=datetime.now(UTC).replace(tzinfo=None),
                )
            ]
        )
    )

    with (
        patch("src.auth.oauth_clients.config.oauth_secret_hash_key", TEST_HASH_KEY),
        patch("src.auth.oauth_clients.get_session", return_value=validation_session),
        patch("src.auth.oauth_clients._touch_last_used", _ignore_touch_last_used),
    ):
        assert await validate_client_credentials("hpc_valid", client_secret) is None


@pytest.mark.asyncio
async def test_validate_client_credentials_rejects_expired_secret() -> None:
    client_secret = "hps_valid_secret"
    validation_session = _FakeSession(
        _FakeResult(
            [
                _validation_client_record(
                    client_secret=client_secret,
                    expires_at=datetime.now(UTC).replace(tzinfo=None)
                    - timedelta(seconds=1),
                )
            ]
        )
    )

    with (
        patch("src.auth.oauth_clients.config.oauth_secret_hash_key", TEST_HASH_KEY),
        patch("src.auth.oauth_clients.get_session", return_value=validation_session),
        patch("src.auth.oauth_clients._touch_last_used", _ignore_touch_last_used),
    ):
        assert await validate_client_credentials("hpc_valid", client_secret) is None


@pytest.mark.asyncio
async def test_validate_client_credentials_rejects_unknown_client() -> None:
    session = _FakeSession(_FakeResult([]))

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        assert await validate_client_credentials("hpc_missing", "hps_secret") is None


@pytest.mark.asyncio
async def test_disable_oauth_client_returns_false_for_missing_client() -> None:
    session = _FakeSession(_FakeResult([]))

    with patch("src.auth.oauth_clients.get_session", return_value=session):
        assert not await disable_oauth_client("hpc_missing")
