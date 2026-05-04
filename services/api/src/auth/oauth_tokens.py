"""HyperP-issued OAuth access tokens and JWKS support."""

from __future__ import annotations

import base64
import binascii
import json
import time
import uuid
from dataclasses import dataclass
from typing import TypedDict, TypeGuard

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.auth.oauth_client_models import OAuthClient
from src.config import AppConfig, config

_CLOCK_SKEW_SECONDS = 300
_DEFAULT_ACCESS_TOKEN_SECONDS = 900
_ALGORITHM = "RS256"


@dataclass(frozen=True, slots=True)
class OAuthClientClaims:
    """Verified claims from a HyperP client-credentials access token."""

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
    entity_key: str | None = None


class JsonWebKey(TypedDict):
    """JSON Web Key fields exposed for token verification."""

    kty: str
    use: str
    kid: str
    alg: str
    n: str
    e: str


class JsonWebKeySet(TypedDict):
    """JSON Web Key Set containing active OAuth signing keys."""

    keys: list[JsonWebKey]


class JwtHeader(TypedDict):
    """JWT JOSE header for HyperP-issued access tokens."""

    alg: str
    typ: str
    kid: str


class JwtPayload(TypedDict, total=False):
    """JWT claims payload for HyperP-issued access tokens."""

    iss: str
    aud: str
    sub: str
    client_id: str
    scope: str
    scopes: list[str]
    entity_key: str | None
    iat: int
    nbf: int
    exp: int
    jti: str


type JsonValue = str | int | list[str] | None
type JsonObject = dict[str, JsonValue]
type JsonSerializableObject = JsonObject | JwtHeader | JwtPayload


def validate_oauth_runtime_config(settings: AppConfig = config) -> None:
    """Validate required OAuth runtime settings are non-empty."""
    required_fields = {
        "OAUTH_SECRET_HASH_KEY": settings.oauth_secret_hash_key,
        "OAUTH_PRIVATE_KEY_PEM": settings.oauth_private_key_pem,
        "OAUTH_PUBLIC_KEY_PEM": settings.oauth_public_key_pem,
        "OAUTH_ACTIVE_KEY_ID": settings.oauth_active_key_id,
        "OAUTH_ISSUER": settings.oauth_issuer,
        "OAUTH_AUDIENCE": settings.oauth_audience,
    }
    missing = [name for name, value in required_fields.items() if not value.strip()]
    if missing:
        raise ValueError(f"Missing OAuth runtime configuration: {', '.join(missing)}")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(value: str) -> bytes:
    padding_len = (-len(value)) % 4
    try:
        return base64.b64decode(value + ("=" * padding_len), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid token encoding") from exc


def _json_b64(payload: JsonSerializableObject) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return _b64url(raw)


def _is_json_object(value: object) -> TypeGuard[JsonObject]:
    if not isinstance(value, dict):
        return False
    return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())


def _is_json_value(value: object) -> TypeGuard[JsonValue]:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _json_object(value: str) -> JsonObject:
    try:
        decoded = _b64url_decode(value)
    except ValueError as exc:
        raise ValueError("invalid token encoding") from exc
    try:
        parsed = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("invalid token json") from exc
    if not _is_json_object(parsed):
        raise ValueError("invalid token json")
    return parsed


def _private_key() -> rsa.RSAPrivateKey:
    if not config.oauth_private_key_pem:
        raise ValueError("OAUTH_PRIVATE_KEY_PEM is required")
    key = serialization.load_pem_private_key(config.oauth_private_key_pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("OAUTH_PRIVATE_KEY_PEM must be an RSA private key")
    return key


def _public_key() -> rsa.RSAPublicKey:
    if not config.oauth_public_key_pem:
        raise ValueError("OAUTH_PUBLIC_KEY_PEM is required")
    key = serialization.load_pem_public_key(config.oauth_public_key_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("OAUTH_PUBLIC_KEY_PEM must be an RSA public key")
    return key


def _active_key_id() -> str:
    if not config.oauth_active_key_id:
        raise ValueError("OAUTH_ACTIVE_KEY_ID is required")
    return config.oauth_active_key_id


def _issuer() -> str:
    if not config.oauth_issuer:
        raise ValueError("OAUTH_ISSUER is required")
    return config.oauth_issuer


def _audience() -> str:
    if not config.oauth_audience:
        raise ValueError("OAUTH_AUDIENCE is required")
    return config.oauth_audience


def issue_client_access_token(
    client: OAuthClient,
    scopes: list[str],
    *,
    expires_in_seconds: int = _DEFAULT_ACCESS_TOKEN_SECONDS,
) -> str:
    """Issue an RS256 JWT access token for an OAuth client."""
    now = int(time.time())
    payload = JwtPayload(
        iss=_issuer(),
        aud=_audience(),
        sub=client.client_id,
        client_id=client.client_id,
        scope=" ".join(scopes),
        scopes=scopes,
        iat=now,
        nbf=now,
        exp=now + expires_in_seconds,
        jti=str(uuid.uuid4()),
    )
    if client.entity_key is not None:
        payload["entity_key"] = client.entity_key

    header = JwtHeader(alg=_ALGORITHM, typ="JWT", kid=_active_key_id())
    signing_input = f"{_json_b64(header)}.{_json_b64(payload)}".encode()
    signature = _private_key().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode()}.{_b64url(signature)}"


def _require_str(payload: JsonObject, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {key} claim")
    return value


def _require_int(payload: JsonObject, key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"invalid {key} claim")
    return value


def _require_scopes(payload: JsonObject) -> list[str]:
    value = payload.get("scopes")
    if not isinstance(value, list) or not all(isinstance(scope, str) for scope in value):
        raise ValueError("invalid scopes claim")
    return value


def _verified_payload(raw_payload: JsonObject) -> JwtPayload:
    payload = JwtPayload(
        iss=_require_str(raw_payload, "iss"),
        aud=_require_str(raw_payload, "aud"),
        sub=_require_str(raw_payload, "sub"),
        client_id=_require_str(raw_payload, "client_id"),
        scope=_require_str(raw_payload, "scope"),
        scopes=_require_scopes(raw_payload),
        iat=_require_int(raw_payload, "iat"),
        nbf=_require_int(raw_payload, "nbf"),
        exp=_require_int(raw_payload, "exp"),
        jti=_require_str(raw_payload, "jti"),
    )
    entity_key = raw_payload.get("entity_key")
    if entity_key is not None:
        if not isinstance(entity_key, str):
            raise ValueError("invalid entity_key claim")
        payload["entity_key"] = entity_key
    return payload


def verify_client_access_token(token: str) -> OAuthClientClaims:
    """Verify a HyperP-issued client access token."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("invalid token format")
    header_raw, payload_raw, sig_raw = parts
    header = _json_object(header_raw)
    if header.get("alg") != _ALGORITHM:
        raise ValueError("unsupported token algorithm")
    if header.get("typ") != "JWT":
        raise ValueError("invalid token type")
    if header.get("kid") != _active_key_id():
        raise ValueError("unknown signing key")

    signing_input = f"{header_raw}.{payload_raw}".encode()
    signature = _b64url_decode(sig_raw)
    try:
        _public_key().verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as exc:
        raise ValueError("invalid token signature") from exc

    payload = _verified_payload(_json_object(payload_raw))
    now = int(time.time())
    if payload["iss"] != _issuer():
        raise ValueError("invalid issuer")
    if payload["aud"] != _audience():
        raise ValueError("invalid audience")
    if now > payload["exp"]:
        raise ValueError("token expired")
    if now + _CLOCK_SKEW_SECONDS < payload["nbf"]:
        raise ValueError("token not yet valid")
    return OAuthClientClaims(
        iss=payload["iss"],
        aud=payload["aud"],
        sub=payload["sub"],
        client_id=payload["client_id"],
        scope=payload["scope"],
        scopes=payload["scopes"],
        iat=payload["iat"],
        nbf=payload["nbf"],
        exp=payload["exp"],
        jti=payload["jti"],
        entity_key=payload.get("entity_key"),
    )


def _int_b64url(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    return _b64url(value.to_bytes(length, "big"))


def build_jwks() -> JsonWebKeySet:
    """Return the configured OAuth public key as JWKS."""
    public_numbers = _public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "kid": _active_key_id(),
                "alg": _ALGORITHM,
                "n": _int_b64url(public_numbers.n),
                "e": _int_b64url(public_numbers.e),
            }
        ]
    }
