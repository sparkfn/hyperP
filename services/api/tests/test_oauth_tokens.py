"""Tests for HyperP-issued OAuth JWT access tokens."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from typing import TypedDict
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from src.auth.oauth_client_models import OAuthClient
from src.auth.oauth_tokens import (
    OAuthClientClaims,
    build_jwks,
    issue_client_access_token,
    verify_client_access_token,
)


class JwtHeaderFixture(TypedDict):
    """JWT header fields used by OAuth token tests."""

    alg: str
    typ: str
    kid: str


class JwtPayloadFixture(TypedDict):
    """JWT payload fields used by OAuth token tests."""

    iss: str
    aud: str
    sub: str
    client_id: str
    scope: str
    scopes: list[str]
    iat: int
    nbf: int
    exp: int
    jti: str


JsonFixtureValue = str | int | list[str]
JsonFixtureObject = dict[str, JsonFixtureValue]


def _pem_pair() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _client() -> OAuthClient:
    return OAuthClient(
        client_id="hpc_test",
        name="Test client",
        entity_key="fundbox",
        scopes=["persons:read", "ingest:write"],
        created_by="admin@example.com",
        created_at=datetime.now(UTC).replace(tzinfo=None),
        disabled_at=None,
        last_used_at=None,
        secrets=[],
    )


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json_b64(payload: JsonFixtureObject) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return _b64url(raw)


def _signed_token(
    private_pem: str,
    *,
    header: JwtHeaderFixture | None = None,
    payload: JwtPayloadFixture | None = None,
) -> str:
    now = int(time.time())
    token_header = header or JwtHeaderFixture(alg="RS256", typ="JWT", kid="kid-test")
    token_payload = payload or JwtPayloadFixture(
        iss="http://issuer",
        aud="hyperp-api",
        sub="hpc_test",
        client_id="hpc_test",
        scope="persons:read",
        scopes=["persons:read"],
        iat=now,
        nbf=now,
        exp=now + 900,
        jti="jti-test",
    )
    signing_input = f"{_json_b64(token_header)}.{_json_b64(token_payload)}".encode()
    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise TypeError("test private key must be RSA")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


def _token_config(
    private_pem: str,
    public_pem: str,
) -> tuple[object, object, object, object, object]:
    return (
        patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem),
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
    )


def test_issue_and_verify_client_access_token() -> None:
    private_pem, public_pem = _pem_pair()

    with (
        patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem),
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
    ):
        token = issue_client_access_token(_client(), ["persons:read"], expires_in_seconds=900)
        claims = verify_client_access_token(token)

    assert isinstance(claims, OAuthClientClaims)
    assert claims.sub == "hpc_test"
    assert claims.client_id == "hpc_test"
    assert claims.scopes == ["persons:read"]
    assert claims.scope == "persons:read"
    assert claims.entity_key == "fundbox"
    assert claims.aud == "hyperp-api"


def test_build_jwks_exposes_public_key_with_kid() -> None:
    _private_pem, public_pem = _pem_pair()

    with (
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
    ):
        jwks = build_jwks()

    assert jwks["keys"][0]["kid"] == "kid-test"
    assert jwks["keys"][0]["kty"] == "RSA"
    assert jwks["keys"][0]["use"] == "sig"
    assert jwks["keys"][0]["alg"] == "RS256"
    assert jwks["keys"][0]["n"]
    assert jwks["keys"][0]["e"]


def test_expired_token_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()

    with (
        patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem),
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
    ):
        token = issue_client_access_token(_client(), ["persons:read"], expires_in_seconds=-1)
        with pytest.raises(ValueError, match="expired"):
            verify_client_access_token(token)


def test_wrong_audience_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()

    with (
        patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem),
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "expected"),
    ):
        token = issue_client_access_token(_client(), ["persons:read"], expires_in_seconds=900)
        with patch("src.auth.oauth_tokens.config.oauth_audience", "different"):
            with pytest.raises(ValueError, match="audience"):
                verify_client_access_token(token)


def test_tampered_signature_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()
    token = _signed_token(private_pem)
    header_raw, payload_raw, _signature_raw = token.split(".")
    tampered_token = f"{header_raw}.{payload_raw}.{_b64url(b'tampered')}"

    with (
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
    ):
        with pytest.raises(ValueError, match="signature"):
            verify_client_access_token(tampered_token)


def test_malformed_token_base64_is_rejected_with_value_error() -> None:
    _private_pem, public_pem = _pem_pair()
    malformed_token = "@@@@.@@@@.a"

    with (
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
    ):
        with pytest.raises(ValueError, match="encoding"):
            verify_client_access_token(malformed_token)


def test_unsupported_alg_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()
    token = _signed_token(
        private_pem,
        header=JwtHeaderFixture(alg="HS256", typ="JWT", kid="kid-test"),
    )

    with patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem):
        with pytest.raises(ValueError, match="algorithm"):
            verify_client_access_token(token)


def test_unknown_kid_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()
    token = _signed_token(
        private_pem,
        header=JwtHeaderFixture(alg="RS256", typ="JWT", kid="kid-other"),
    )

    with (
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
    ):
        with pytest.raises(ValueError, match="signing key"):
            verify_client_access_token(token)


def test_future_nbf_beyond_skew_is_rejected() -> None:
    private_pem, public_pem = _pem_pair()
    now = int(time.time())
    payload = JwtPayloadFixture(
        iss="http://issuer",
        aud="hyperp-api",
        sub="hpc_test",
        client_id="hpc_test",
        scope="persons:read",
        scopes=["persons:read"],
        iat=now,
        nbf=now + 600,
        exp=now + 900,
        jti="jti-test",
    )
    token = _signed_token(private_pem, payload=payload)

    with (
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
    ):
        with pytest.raises(ValueError, match="not yet valid"):
            verify_client_access_token(token)
