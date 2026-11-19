"""NRIC normalization, hashing, masking and decryption helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken

from src.config import config

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_NRIC_PATTERN = re.compile(r"(?i)[STFG]\d{7}[A-Z]")
_ENCRYPTION_DOMAIN = b"hyperp-nric-encryption:v1\x00"

NRIC_PLACEHOLDER = "•••••••••"


class NricKeyUnavailableError(Exception):
    """Raised when NRIC encryption key is missing or ciphertext cannot be decrypted."""


def canonical_nric(raw: str) -> str:
    """Return canonical NRIC: uppercase with leading X/x markers stripped."""
    return re.sub(r"^[xX]+", "", raw.strip()).upper()


def is_nric_digest(value: str) -> bool:
    """Return True if value matches a 64-character lowercase hex digest."""
    return bool(_DIGEST_PATTERN.fullmatch(value))


def mask_nric(canonical: str) -> str:
    """Format canonical NRIC into first char + 4 bullets + last 4 chars, or 9 bullets."""
    if len(canonical) == 9:
        return f"{canonical[0]}••••{canonical[5:]}"
    return NRIC_PLACEHOLDER


def format_masked_nric(value: str | None) -> str | None:
    """Return masked NRIC representation.

    Passes through values already containing '•', formats 9-char plaintext to
    masked representation, and replaces digests or unexpected shapes with bullet placeholder.
    """
    if value is None:
        return None
    if "•" in value:
        return value
    if is_nric_digest(value):
        return NRIC_PLACEHOLDER
    canonical = canonical_nric(value)
    return mask_nric(canonical)


def masked_for(value: str | None, nric_masked: str | None = None) -> str | None:
    """Resolve masked value for NRIC display and golden-profile writers.

    Digest-shaped values use nric_masked if present, else None (never mask a digest).
    Non-digest values are treated as legacy plaintext and masked.
    """
    if value is None:
        return None
    if is_nric_digest(value):
        return nric_masked if nric_masked is not None else None
    return format_masked_nric(value)


def mask_identifier_value(identifier_type: str, value: str, nric_masked: str | None = None) -> str:
    """Return display value for an identifier, masking NRIC values."""
    if identifier_type == "nric":
        return masked_for(value, nric_masked) or NRIC_PLACEHOLDER
    return value


def scrub_nric_text(text: str) -> str:
    """Scrub raw NRIC tokens in free-form text by masking them."""

    def _replace_match(match: re.Match[str]) -> str:
        return mask_nric(match.group(0).upper())

    return _NRIC_PATTERN.sub(_replace_match, text)


def nric_digest(canonical: str, key: str | None = None) -> str | None:
    """Compute HMAC-SHA256 digest of canonical NRIC.

    Returns None if no HMAC key is configured.
    """
    hmac_key = key if key is not None else config.nric_hmac_key
    if not hmac_key:
        return None
    key_bytes = hmac_key.encode("utf-8")
    return hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def derive_fernet_key(key: str) -> bytes:
    """Derive Fernet encryption key from NRIC HMAC key using domain separation."""
    if not key:
        raise NricKeyUnavailableError("NRIC HMAC key is not configured.")
    key_bytes = key.encode("utf-8")
    digest = hashlib.sha256(_ENCRYPTION_DOMAIN + key_bytes).digest()
    return base64.urlsafe_b64encode(digest)


def decrypt_nric(ciphertext: str, key: str | None = None) -> str:
    """Decrypt NRIC ciphertext using derived Fernet key."""
    hmac_key = key if key is not None else config.nric_hmac_key
    if not hmac_key:
        raise NricKeyUnavailableError("NRIC HMAC key is not configured.")
    try:
        fernet = Fernet(derive_fernet_key(hmac_key))
        decrypted = fernet.decrypt(ciphertext.encode("ascii"))
        return decrypted.decode("utf-8")
    except (InvalidToken, UnicodeError) as exc:
        raise NricKeyUnavailableError("Failed to decrypt NRIC ciphertext.") from exc
