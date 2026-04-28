"""Google ID token verification with clock-skew tolerance."""

from __future__ import annotations

import base64
import json
import time

import google.auth.transport.requests as google_requests
from pydantic import BaseModel

from src.config import config

# Clock skew tolerance — covers real-world drift between our server
# and Google's token-issuing servers. Google itself recommends 5 minutes;
# we use 5 minutes so any plausible drift is absorbed.
_NBF_TOLERANCE_SECS = 300


class GoogleClaims(BaseModel):
    email: str
    sub: str
    name: str | None = None
    email_verified: bool = False
    hd: str | None = None


_request_adapter: google_requests.Request | None = None


def _get_request() -> google_requests.Request:
    global _request_adapter
    if _request_adapter is None:
        _request_adapter = google_requests.Request()
    return _request_adapter


def _base64url_decode(data: str) -> bytes:
    """Decode a base64url string (with or without padding)."""
    # Add padding as needed
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data)


def _base64url_encode(data: bytes) -> str:
    """Encode bytes to base64url (no padding)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _decode_jwt_parts(token: str) -> tuple[dict, dict, bytes]:
    """Split JWT into (header, payload, signature) — no verification."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("Wrong number of segments in token")
    header = json.loads(_base64url_decode(parts[0]))
    payload = json.loads(_base64url_decode(parts[1]))
    signature = _base64url_decode(parts[2])
    return header, payload, signature


def _get_google_public_certs() -> dict[str, str]:
    """Fetch Google's RSA public key certificates from their well-known endpoint."""
    request = _get_request()
    response = request(url="https://www.googleapis.com/oauth2/v1/certs")
    if response.status != 200:
        raise ValueError(f"Failed to fetch Google certs: HTTP {response.status}")
    certs: dict[str, str] = json.loads(response.data.decode("utf-8"))
    return certs


def _verify_rs256(signed_bytes: bytes, signature: bytes, public_key_pem: str) -> bool:
    """Verify an RS256 JWT signature using a PEM-encoded RSA public key."""
    from google.auth.crypt import rsa

    verifier = rsa.RSAVerifier.from_string(public_key_pem)
    return verifier.verify(signed_bytes, signature)


def verify_google_id_token(token: str) -> GoogleClaims:
    """Verify a Google OAuth 2.0 ID token. Raises ValueError on failure."""
    client_id = config.google_oauth_client_id
    if not client_id:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID is not configured")

    # --- Step 1: parse raw JWT ---
    header, payload, signature = _decode_jwt_parts(token)

    # --- Step 2: validate algorithm ---
    alg = header.get("alg")
    if alg != "RS256":
        raise ValueError(f"Unsupported signature algorithm {alg}")

    # --- Step 3: apply nbf tolerance ---
    now = time.time()
    nbf_raw = payload.get("nbf")
    if nbf_raw is not None:
        if not isinstance(nbf_raw, (int, float)):
            raise ValueError("nbf claim must be a numeric timestamp")
        nbf = float(nbf_raw)
        drift = nbf - now
        if drift > _NBF_TOLERANCE_SECS:
            raise ValueError(
                f"Token not yet valid (nbf={nbf:.0f}, now={now:.0f}, drift={drift:.1f}s). "
                "Check server clock."
            )
        if drift > 0:
            # Token's nbf is slightly in the future — backdate it by the
            # tolerance so the expiry check below doesn't reject it.
            payload["nbf"] = nbf - _NBF_TOLERANCE_SECS

    # --- Step 4: check standard expiry ---
    exp_raw = payload.get("exp")
    if exp_raw is not None:
        if not isinstance(exp_raw, (int, float)):
            raise ValueError("exp claim must be a numeric timestamp")
        exp = float(exp_raw)
        if now > exp:
            raise ValueError(f"Token expired at {exp} (now={now:.0f})")

    # --- Step 5: verify signature with Google's public keys ---
    kid = header.get("kid")
    certs = _get_google_public_certs()

    # Build the signed content using the original (unmodified) header+payload
    signed_content = (token.split(".")[0] + "." + token.split(".")[1]).encode()

    if kid:
        if kid not in certs:
            raise ValueError(f"Token kid '{kid}' not found in Google certs")
        if not _verify_rs256(signed_content, signature, certs[kid]):
            raise ValueError("Token signature verification failed")
    else:
        # Token without kid — try all certs
        verified = False
        for cert_pem in certs.values():
            if _verify_rs256(signed_content, signature, cert_pem):
                verified = True
                break
        if not verified:
            raise ValueError("Token signature verification failed (no matching cert)")

    # --- Step 6: check audience and hosted domain ---
    aud = payload.get("aud")
    if aud != client_id:
        raise ValueError(f"Token audience '{aud}' does not match client ID")

    if not payload.get("email_verified"):
        raise ValueError("Google reports this email as unverified")

    hosted = config.google_oauth_hosted_domain
    if hosted and payload.get("hd") != hosted:
        raise ValueError(f"account is not in the required hosted domain '{hosted}'")

    # --- Step 7: return parsed claims ---
    return GoogleClaims(
        email=payload["email"].lower(),
        sub=payload["sub"],
        name=payload.get("name"),
        email_verified=True,
        hd=payload.get("hd") if isinstance(payload.get("hd"), str) else None,
    )
