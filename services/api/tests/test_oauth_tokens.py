"""Tests for HyperP-issued OAuth JWT access tokens."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime
from typing import TypedDict
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi.testclient import TestClient
from src.app import build_app
from src.auth.oauth_client_models import OAuthClient, OAuthClientSecret
from src.auth.oauth_tokens import (
    JsonWebKeySet,
    OAuthClientClaims,
    build_jwks,
    issue_client_access_token,
    verify_client_access_token,
)
from src.routes.oauth import SUNSET_HTTP_DATE


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
    secret_id: str


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
        secret_id="sec_test",
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
        token = issue_client_access_token(
            _client(), ["persons:read"], secret_id="sec_test", expires_in_seconds=900
        )
        claims = verify_client_access_token(token)

    assert isinstance(claims, OAuthClientClaims)
    assert claims.sub == "hpc_test"
    assert claims.client_id == "hpc_test"
    assert claims.scopes == ["persons:read"]
    assert claims.scope == "persons:read"
    assert claims.entity_key == "fundbox"
    assert claims.aud == "hyperp-api"


def test_token_carries_and_verifies_secret_id() -> None:
    private_pem, public_pem = _pem_pair()

    with (
        patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem),
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
    ):
        token = issue_client_access_token(
            _client(), ["persons:read"], secret_id="sec_1", expires_in_seconds=600
        )
        claims = verify_client_access_token(token)

    assert claims.secret_id == "sec_1"


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
        token = issue_client_access_token(
            _client(), ["persons:read"], secret_id="sec_test", expires_in_seconds=-1
        )
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
        token = issue_client_access_token(
            _client(), ["persons:read"], secret_id="sec_test", expires_in_seconds=900
        )
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
        secret_id="sec_test",
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


def test_legacy_and_canonical_spellings_issue_verifiable_tokens_and_jwks() -> None:
    private_pem, public_pem = _pem_pair()
    registered_client = _client().model_copy(
        update={
            "secrets": [
                OAuthClientSecret(
                    secret_id="sec_t1",
                    secret_prefix="hps_xxxxxx",
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                    revoked_at=None,
                    last_used_at=None,
                )
            ]
        }
    )
    client = TestClient(build_app())
    jwks_bodies: list[JsonWebKeySet] = []

    with (
        patch("src.auth.oauth_tokens.config.oauth_private_key_pem", private_pem),
        patch("src.auth.oauth_tokens.config.oauth_public_key_pem", public_pem),
        patch("src.auth.oauth_tokens.config.oauth_active_key_id", "kid-test"),
        patch("src.auth.oauth_tokens.config.oauth_issuer", "http://issuer"),
        patch("src.auth.oauth_tokens.config.oauth_audience", "hyperp-api"),
        patch(
            "src.routes.oauth.validate_client_credentials",
            new=AsyncMock(return_value=(registered_client, ["persons:read"])),
        ),
        patch("src.routes.oauth.register_token", new=AsyncMock(return_value=None)),
    ):
        for token_path, jwks_path, legacy in (
            ("/v1/oauth/token", "/v1/oauth/jwks", True),
            ("/oauth2/v1/token", "/oauth2/v1/jwks", False),
        ):
            expected_sunset = SUNSET_HTTP_DATE if legacy else None

            token_res = client.post(
                token_path,
                data={
                    "grant_type": "client_credentials",
                    "client_id": "hpc_test",
                    "client_secret": "hps_secret",
                },
            )
            assert token_res.status_code == 200
            assert token_res.headers.get("Sunset") == expected_sunset
            claims = verify_client_access_token(token_res.json()["access_token"])
            assert claims.client_id == "hpc_test"
            assert claims.scopes == ["persons:read"]
            assert claims.secret_id == "sec_t1"
            assert claims.aud == "hyperp-api"

            jwks_res = client.get(jwks_path)
            assert jwks_res.status_code == 200
            assert jwks_res.headers.get("Sunset") == expected_sunset
            keys = jwks_res.json()["keys"]
            assert len(keys) == 1
            assert keys[0]["kid"] == "kid-test"
            assert keys[0]["alg"] == "RS256"
            assert keys[0]["kty"] == "RSA"
            assert keys[0]["use"] == "sig"
            jwks_bodies.append(jwks_res.json())

    assert jwks_bodies[0] == jwks_bodies[1]
