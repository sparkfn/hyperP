"""The Google-token cache must not serve a different token sharing a jti."""

from __future__ import annotations

import pytest
from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from src.auth import deps
from src.auth.models import AuthUser


def _make_request() -> Request:
    return Request({"type": "http", "headers": []})


@pytest.mark.asyncio
async def test_cache_miss_when_token_differs_for_same_jti(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps.config, "auth_enabled", True)
    deps._USER_CACHE.clear()

    cached_user = AuthUser(email="victim@x", google_sub="v", role="admin", entity_key=None, display_name="V")
    deps._USER_CACHE["shared-jti"] = ("real-token", cached_user)

    async def _not_revoked(_jti: str) -> bool:
        return False

    def _claims(_token: str) -> tuple[str | None, int | None]:
        return "shared-jti", None

    async def _verify_fails(_token: str) -> object:
        raise ValueError("bad signature")

    monkeypatch.setattr(deps, "is_token_revoked", _not_revoked)
    monkeypatch.setattr(deps, "decode_jwt_claims", _claims)
    monkeypatch.setattr(deps, "verify_google_id_token", _verify_fails)

    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="forged-token")
    with pytest.raises(HTTPException):
        await deps.get_current_user(_make_request(), creds)
