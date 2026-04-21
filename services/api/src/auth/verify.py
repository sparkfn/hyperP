"""Google ID token verification."""

from __future__ import annotations

from typing import TypedDict

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from pydantic import BaseModel

from src.config import config


class GoogleClaims(BaseModel):
    email: str
    sub: str
    name: str | None = None
    email_verified: bool = False
    hd: str | None = None


class _RequiredClaims(TypedDict):
    email: str
    sub: str


class _OptionalClaims(TypedDict, total=False):
    name: str
    email_verified: bool
    hd: str


class _IdTokenPayload(_RequiredClaims, _OptionalClaims):
    pass


_request_adapter: google_requests.Request | None = None


def _get_request() -> google_requests.Request:
    global _request_adapter
    if _request_adapter is None:
        _request_adapter = google_requests.Request()
    return _request_adapter


def verify_google_id_token(token: str) -> GoogleClaims:
    """Verify an OAuth 2.0 ID token. Raises ValueError on failure."""
    client_id = config.google_oauth_client_id
    if not client_id:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID is not configured")
    # google-auth returns dict[str, Any]; typed as our narrowed payload shape
    data: _IdTokenPayload = google_id_token.verify_oauth2_token(  # type: ignore[assignment]
        token, _get_request(), audience=client_id
    )
    if not data.get("email_verified"):
        raise ValueError("Google reports this email as unverified")
    hosted = config.google_oauth_hosted_domain
    if hosted and data.get("hd") != hosted:
        raise ValueError(f"account is not in the required hosted domain '{hosted}'")
    email = data["email"].lower()
    hd = data.get("hd")
    return GoogleClaims(
        email=email,
        sub=data["sub"],
        name=data.get("name"),
        email_verified=True,
        hd=hd if isinstance(hd, str) else None,
    )
