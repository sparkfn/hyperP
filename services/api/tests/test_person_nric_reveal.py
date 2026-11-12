"""Tests for audited NRIC reveal route and repository."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from src.auth.deps import get_current_user_or_oauth_client
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClientUser
from src.frontend_app import build_frontend_app
from src.nric import (
    NricKeyUnavailableError,
    derive_fernet_key,
)
from src.oauth2_app import build_oauth2_app
from src.repositories.deps import get_nric_repo
from src.repositories.neo4j.nric import Neo4jNricRevealRepository, _reveal_tx
from src.repositories.protocols.nric import (
    NricRevealRepository,
    NricRevealResult,
    NricUnavailableError,
)

TEST_HMAC_KEY = "test-nric-hmac-key-v1"


def _encrypt_for_test(canonical: str, key: str = TEST_HMAC_KEY) -> str:
    fernet = Fernet(derive_fernet_key(key))
    return fernet.encrypt(canonical.encode("utf-8")).decode("ascii")


def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
    )


def _employee_user() -> AuthUser:
    return AuthUser(
        email="employee@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key=None,
        display_name="Employee",
    )


def _admin_oauth_client() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:admin_client",
        google_sub="admin_client",
        role="admin",
        entity_key=None,
        display_name="Admin Machine Client",
        source="oauth_client",
        client_id="admin_client",
        key_scopes=["admin"],
    )


class _MockNricRepo(NricRevealRepository):
    def __init__(
        self,
        result: NricRevealResult | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.result = result
        self.exc = exc
        self.calls: list[dict[str, str]] = []

    async def reveal(
        self,
        person_id: str,
        actor_email: str,
        reason: str,
        request_id: str,
    ) -> NricRevealResult | None:
        self.calls.append(
            {
                "person_id": person_id,
                "actor_email": actor_email,
                "reason": reason,
                "request_id": request_id,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.result


def test_reveal_route_admin_success_passes_actor_email() -> None:
    expected = NricRevealResult(
        person_id="p-1",
        reveal_id="rev-1",
        revealed_at="2026-05-01T00:00:00Z",
        values=["S1234567D"],
        unavailable_count=0,
    )
    repo = _MockNricRepo(result=expected)
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _admin_user
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/p-1/nric/reveal",
        json={"reason": "Compliance audit for customer review"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["person_id"] == "p-1"
    assert data["reveal_id"] == "rev-1"
    assert data["values"] == ["S1234567D"]
    assert data["unavailable_count"] == 0

    assert len(repo.calls) == 1
    assert repo.calls[0]["actor_email"] == "admin@example.com"
    assert repo.calls[0]["reason"] == "Compliance audit for customer review"


def test_reveal_route_non_admin_human_forbidden() -> None:
    repo = _MockNricRepo()
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _employee_user
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/p-1/nric/reveal",
        json={"reason": "Audit request"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_reveal_route_oauth_client_forbidden() -> None:
    repo = _MockNricRepo()
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _admin_oauth_client
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/p-1/nric/reveal",
        json={"reason": "Audit request"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_oauth"


def test_reveal_route_not_present_on_oauth2_app() -> None:
    oauth_app = build_oauth2_app()
    client = TestClient(oauth_app)
    response = client.post(
        "/oauth2/v1/persons/p-1/nric/reveal",
        json={"reason": "Audit request"},
    )
    # Route is not mounted on machine surface
    assert response.status_code in {404, 405}


def test_reveal_route_person_not_found() -> None:
    repo = _MockNricRepo(result=None)
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _admin_user
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/missing/nric/reveal",
        json={"reason": "Audit request"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "person_not_found"


def test_reveal_route_key_unavailable() -> None:
    repo = _MockNricRepo(exc=NricKeyUnavailableError("Key missing"))
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _admin_user
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/p-1/nric/reveal",
        json={"reason": "Audit request"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "nric_key_unavailable"


def test_reveal_route_all_unavailable() -> None:
    repo = _MockNricRepo(exc=NricUnavailableError("All NRIC values unavailable"))
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _admin_user
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/p-1/nric/reveal",
        json={"reason": "Audit request"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "nric_unavailable"


def test_reveal_route_database_failure() -> None:
    repo = _MockNricRepo(exc=RuntimeError("Database query failed"))
    app = build_frontend_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _admin_user
    app.dependency_overrides[get_nric_repo] = lambda: repo

    client = TestClient(app)
    response = client.post(
        "/app/v2/persons/p-1/nric/reveal",
        json={"reason": "Audit request"},
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "nric_reveal_failed"


@pytest.mark.asyncio
async def test_repo_reveal_delegates_to_write_session() -> None:
    mock_session = AsyncMock()
    expected = NricRevealResult(
        person_id="p-1",
        reveal_id="rev-1",
        revealed_at="2026-05-01T00:00:00Z",
        values=["S1234567D"],
        unavailable_count=0,
    )
    mock_session.execute_write = AsyncMock(return_value=expected)
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    mock_session_ctx.__aexit__.return_value = None

    repo = Neo4jNricRevealRepository()
    with patch("src.repositories.neo4j.nric.get_session", return_value=mock_session_ctx):
        result = await repo.reveal(
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Audit",
            request_id="req-1",
        )

    assert result == expected
    mock_session.execute_write.assert_awaited_once_with(
        _reveal_tx, "p-1", "admin@example.com", "Audit", "req-1"
    )


@pytest.mark.asyncio
async def test_reveal_tx_success_and_scrubs_reason() -> None:
    ciphertext = _encrypt_for_test("S1234567D")
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"

    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": digest, "nric_encrypted": ciphertext},
            ],
        }
    )
    mock_event_cursor = MagicMock()
    mock_event_cursor.consume = AsyncMock()
    mock_tx.run = AsyncMock(side_effect=[mock_cursor, mock_event_cursor])

    with patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY):
        result = await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Audited review for S7012164F request",
            request_id="req-123",
        )

    assert result is not None
    assert result.person_id == "p-1"
    assert result.values == ["S1234567D"]
    assert result.unavailable_count == 0

    # Verify audit event write was executed on the same tx
    calls = mock_tx.run.call_args_list
    assert len(calls) == 2
    event_call_kwargs = calls[1].kwargs
    assert event_call_kwargs["person_id"] == "p-1"
    assert event_call_kwargs["actor_email"] == "admin@example.com"
    assert event_call_kwargs["value_count"] == 1
    assert event_call_kwargs["unavailable_count"] == 0
    # Reason must have S7012164F masked
    assert "S7012164F" not in event_call_kwargs["reason"]
    assert "S••••164F" in event_call_kwargs["reason"]


@pytest.mark.asyncio
async def test_reveal_tx_deduplicates_dual_node_person() -> None:
    ciphertext = _encrypt_for_test("S1234567D")
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"

    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": "S1234567D", "nric_encrypted": None},
                {"normalized_value": digest, "nric_encrypted": ciphertext},
            ],
        }
    )
    mock_event_cursor = MagicMock()
    mock_event_cursor.consume = AsyncMock()
    mock_tx.run = AsyncMock(side_effect=[mock_cursor, mock_event_cursor])

    with patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY):
        result = await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Testing dedupe",
            request_id="req-1",
        )

    assert result is not None
    assert result.values == ["S1234567D"]
    assert result.unavailable_count == 0

    event_kwargs = mock_tx.run.call_args_list[1].kwargs
    assert event_kwargs["value_count"] == 1
    assert event_kwargs["unavailable_count"] == 0


@pytest.mark.asyncio
async def test_reveal_tx_partial_case_with_c1_row() -> None:
    digest_c1 = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"

    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": "T0123456G", "nric_encrypted": None},
                {"normalized_value": digest_c1, "nric_encrypted": None},
            ],
        }
    )
    mock_event_cursor = MagicMock()
    mock_event_cursor.consume = AsyncMock()
    mock_tx.run = AsyncMock(side_effect=[mock_cursor, mock_event_cursor])

    with patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY):
        result = await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Partial case",
            request_id="req-1",
        )

    assert result is not None
    assert result.values == ["T0123456G"]
    assert result.unavailable_count == 1
    assert digest_c1 not in result.values

    # Check event kwargs have unavailable_count == 1 and value_count == 1
    event_kwargs = mock_tx.run.call_args_list[1].kwargs
    assert event_kwargs["value_count"] == 1
    assert event_kwargs["unavailable_count"] == 1


@pytest.mark.asyncio
async def test_reveal_tx_all_unavailable_raises_and_writes_no_event() -> None:
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"

    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": digest, "nric_encrypted": None},
            ],
        }
    )
    mock_tx.run = AsyncMock(return_value=mock_cursor)

    with (
        patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY),
        pytest.raises(NricUnavailableError),
    ):
        await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="All unavailable",
            request_id="req-1",
        )

    assert mock_tx.run.call_count == 1


@pytest.mark.asyncio
async def test_reveal_tx_key_missing_with_ciphertext_raises_key_unavailable() -> None:
    ciphertext = _encrypt_for_test("S1234567D")
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"

    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": digest, "nric_encrypted": ciphertext},
            ],
        }
    )
    mock_tx.run = AsyncMock(return_value=mock_cursor)

    with (
        patch("src.nric.config.nric_hmac_key", ""),
        pytest.raises(NricKeyUnavailableError),
    ):
        await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Key missing",
            request_id="req-1",
        )

    # Event write was never executed
    assert mock_tx.run.call_count == 1


@pytest.mark.asyncio
async def test_reveal_tx_invalid_ciphertext_raises_key_unavailable() -> None:
    digest = "a43bb81909a911bc898702d96b9cb10a382793e492d8175abf54772f8df60b2a"

    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": digest, "nric_encrypted": "invalid-fernet-token"},
            ],
        }
    )
    mock_tx.run = AsyncMock(return_value=mock_cursor)

    with (
        patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY),
        pytest.raises(NricKeyUnavailableError),
    ):
        await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Corrupted token",
            request_id="req-1",
        )

    assert mock_tx.run.call_count == 1


@pytest.mark.asyncio
async def test_reveal_tx_key_missing_with_only_legacy_rows_succeeds() -> None:
    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": "S1234567D", "nric_encrypted": None},
            ],
        }
    )
    mock_event_cursor = MagicMock()
    mock_event_cursor.consume = AsyncMock()
    mock_tx.run = AsyncMock(side_effect=[mock_cursor, mock_event_cursor])

    with patch("src.nric.config.nric_hmac_key", ""):
        result = await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Legacy only",
            request_id="req-1",
        )

    assert result is not None
    assert result.values == ["S1234567D"]
    assert result.unavailable_count == 0
    assert mock_tx.run.call_count == 2
    event_kwargs = mock_tx.run.call_args_list[1].kwargs
    assert event_kwargs["value_count"] == 1


@pytest.mark.asyncio
async def test_reveal_tx_audit_write_failure_propagates_exception() -> None:
    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [
                {"normalized_value": "S1234567D", "nric_encrypted": None},
            ],
        }
    )
    mock_tx.run = AsyncMock(
        side_effect=[mock_cursor, RuntimeError("Audit insert constraint failure")]
    )

    with (
        patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY),
        pytest.raises(RuntimeError, match="Audit insert constraint failure"),
    ):
        await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="Audit failure",
            request_id="req-1",
        )


@pytest.mark.asyncio
async def test_reveal_tx_unknown_or_merged_person_returns_none() -> None:
    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(return_value=None)
    mock_tx.run = AsyncMock(return_value=mock_cursor)

    result = await _reveal_tx(
        mock_tx,
        person_id="merged-p",
        actor_email="admin@example.com",
        reason="Check",
        request_id="req-1",
    )
    assert result is None
    assert mock_tx.run.call_count == 1


@pytest.mark.asyncio
async def test_reveal_tx_zero_nric_identifiers_writes_event_and_returns_empty() -> None:
    mock_tx = AsyncMock()
    mock_cursor = MagicMock()
    mock_cursor.single = AsyncMock(
        return_value={
            "person_id": "p-1",
            "identifiers": [],
        }
    )
    mock_event_cursor = MagicMock()
    mock_event_cursor.consume = AsyncMock()
    mock_tx.run = AsyncMock(side_effect=[mock_cursor, mock_event_cursor])

    with patch("src.nric.config.nric_hmac_key", TEST_HMAC_KEY):
        result = await _reveal_tx(
            mock_tx,
            person_id="p-1",
            actor_email="admin@example.com",
            reason="No nric",
            request_id="req-1",
        )

    assert result is not None
    assert result.values == []
    assert result.unavailable_count == 0
    assert mock_tx.run.call_count == 2
    event_kwargs = mock_tx.run.call_args_list[1].kwargs
    assert event_kwargs["value_count"] == 0
    assert event_kwargs["unavailable_count"] == 0
