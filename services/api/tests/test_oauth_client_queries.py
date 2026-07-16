from __future__ import annotations

from src.graph.queries.oauth_clients import (
    CREATE_OAUTH_CLIENT_WITH_SECRET,
    GET_OAUTH_CLIENT_FOR_VALIDATION,
    ROTATE_OAUTH_CLIENT_SECRET,
    UPDATE_OAUTH_CLIENT,
)


def test_create_sets_ttl_and_no_secret_expiry() -> None:
    assert "access_token_ttl_seconds" in CREATE_OAUTH_CLIENT_WITH_SECRET
    assert "secret_expires_at" not in CREATE_OAUTH_CLIENT_WITH_SECRET
    assert "expires_at" not in CREATE_OAUTH_CLIENT_WITH_SECRET


def test_validation_query_only_returns_active_secret() -> None:
    assert "revoked_at IS NULL" in GET_OAUTH_CLIENT_FOR_VALIDATION


def test_rotate_revokes_previous_and_creates_new() -> None:
    assert "revoked_at = datetime()" in ROTATE_OAUTH_CLIENT_SECRET
    assert "CREATE (s:OAuthClientSecret" in ROTATE_OAUTH_CLIENT_SECRET


def test_update_sets_ttl() -> None:
    assert "access_token_ttl_seconds" in UPDATE_OAUTH_CLIENT
