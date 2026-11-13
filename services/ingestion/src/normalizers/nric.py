"""NRIC / FIN government-identifier normalization, validation, masking, and crypto.

The canonical normalized value is the upper-cased identifier with junk ``x`` /
``X`` edge markers removed. Checksum validation is available via
:func:`nric_checksum_valid` and recorded on stored Identifier nodes, but not
enforced during ingestion — upstream data is not guaranteed to be checksum-clean.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from cryptography.fernet import Fernet

from src.models import QualityFlag
from src.normalizers.clean import strip_leading_x_markers

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_NRIC_PATTERN = re.compile(r"^[STFG]\d{7}[A-Z]$")

_ST_LETTERS = "JZIHGFEDCBA"
_FG_LETTERS = "XWUTRQPNMLK"
_WEIGHTS = (2, 7, 6, 5, 4, 3, 2)


def normalize_nric(raw: str) -> tuple[str | None, QualityFlag]:
    """Return ``(normalized_value, quality_flag)`` for a raw NRIC/FIN string.

    Strips surrounding whitespace and leading junk ``x``/``X`` markers (e.g.
    ``xxxS7012164F`` -> ``S7012164F``), then upper-cases every character
    (``s8929303j`` -> ``S8929303J``). Trailing ``x``/``X`` is preserved because a
    valid check letter may itself be ``X``. Returns ``(None, 'invalid_format')``
    when nothing usable remains.
    """
    cleaned = strip_leading_x_markers(raw).upper()
    if not cleaned:
        return None, QualityFlag.INVALID_FORMAT
    return cleaned, QualityFlag.VALID


def is_nric_digest(value: str) -> bool:
    """Return True if ``value`` is a 64-char lowercase hex HMAC-SHA256 digest."""
    return bool(_DIGEST_PATTERN.fullmatch(value))


def nric_checksum_valid(canonical: str) -> bool:
    """Validate Singapore NRIC/FIN checksum according to the official modulo-11 algorithm.

    Supported prefixes:
    - S: offset 0, check letters JZIHGFEDCBA
    - T: offset +4, check letters JZIHGFEDCBA
    - F: offset 0, check letters XWUTRQPNMLK
    - G: offset +4, check letters XWUTRQPNMLK

    M-series and any other prefixes are out of scope and evaluate to False.
    Placeholder values (7 identical digits) evaluate to False.
    """
    if not _NRIC_PATTERN.fullmatch(canonical):
        return False
    digits = canonical[1:8]
    # Placeholder check: 7 identical digits fail closed even if check letter matches
    if len(set(digits)) == 1:
        return False

    prefix = canonical[0]
    last_char = canonical[8]
    offset = 4 if prefix in ("T", "G") else 0
    total = sum(int(d) * w for d, w in zip(digits, _WEIGHTS, strict=True)) + offset
    remainder = total % 11
    expected_letter = _ST_LETTERS[remainder] if prefix in ("S", "T") else _FG_LETTERS[remainder]
    return last_char == expected_letter


def mask_nric(canonical: str) -> str:
    """Return a masked representation of an NRIC.

    A 9-character canonical NRIC is masked as first char + 4 bullets + last 4 chars
    (e.g. ``S••••567D``). Any other length is masked as 9 bullets (``•••••••••``).
    """
    if len(canonical) == 9:
        return f"{canonical[0]}••••{canonical[5:]}"
    return "•••••••••"


def masked_for(value: str | None, nric_masked: str | None) -> str | None:
    """Derive the masked NRIC value for golden profile or display storage.

    - Digest-shaped -> ``nric_masked`` if present, else None.
    - Not digest-shaped (legacy plaintext) -> ``mask_nric(value)`` if non-empty, else None.
    """
    if value is None or not value:
        return None
    if is_nric_digest(value):
        return nric_masked if nric_masked else None
    return mask_nric(value)


def nric_digest(canonical: str, key: str) -> str:
    """Compute 64-character lowercase hex HMAC-SHA256 digest of canonical NRIC."""
    key_bytes = key.encode("utf-8")
    return hmac.new(key_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _fernet_cipher(key: str) -> Fernet:
    """Derive a Fernet cipher from an HMAC key using domain-separated SHA-256."""
    key_bytes = key.encode("utf-8")
    derived = base64.urlsafe_b64encode(
        hashlib.sha256(b"hyperp-nric-encryption:v1\x00" + key_bytes).digest()
    )
    return Fernet(derived)


def encrypt_nric(canonical: str, key: str) -> str:
    """Encrypt a canonical NRIC using Fernet with domain-separated derived key."""
    cipher = _fernet_cipher(key)
    ciphertext_bytes = cipher.encrypt(canonical.encode("utf-8"))
    return ciphertext_bytes.decode("ascii")
