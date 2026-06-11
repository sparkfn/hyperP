from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.auth.oauth_client_models import (
    ACCESS_TOKEN_TTL_MAX_SECONDS,
    ACCESS_TOKEN_TTL_MIN_SECONDS,
    CreateOAuthClientRequest,
    OAuthAccessTokenView,
    UpdateOAuthClientRequest,
)


def test_create_request_accepts_ttl_in_bounds() -> None:
    req = CreateOAuthClientRequest(name="x", scopes=["persons:read"], access_token_ttl_seconds=900)
    assert req.access_token_ttl_seconds == 900


def test_create_request_rejects_ttl_below_min() -> None:
    with pytest.raises(ValidationError):
        CreateOAuthClientRequest(name="x", scopes=["persons:read"],
                                 access_token_ttl_seconds=ACCESS_TOKEN_TTL_MIN_SECONDS - 1)


def test_create_request_rejects_ttl_above_max() -> None:
    with pytest.raises(ValidationError):
        CreateOAuthClientRequest(name="x", scopes=["persons:read"],
                                 access_token_ttl_seconds=ACCESS_TOKEN_TTL_MAX_SECONDS + 1)


def test_update_request_all_optional() -> None:
    req = UpdateOAuthClientRequest()
    assert req.name is None and req.scopes is None and req.access_token_ttl_seconds is None


def test_access_token_view_shape() -> None:
    view = OAuthAccessTokenView(jti="t1", scope="persons:read", issued_at=1, expires_at=2,
                                last_used_at=None, last_used_ip=None)
    assert view.jti == "t1"
