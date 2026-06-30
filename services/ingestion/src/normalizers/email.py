"""Email normalization and validation."""

from __future__ import annotations

import re

from src.models import QualityFlag

# Deliberately permissive — catches most real-world addresses.
_EMAIL_RE = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)*\.[a-zA-Z]{2,}$"
)

_PLACEHOLDER_PATTERNS = frozenset(
    {
        "test@test.com",
        "na@na.com",
        "noreply@noreply.com",
        "unknown@unknown.com",
        "test@example.com",
        "null@null.com",
    }
)


_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def _canonicalize_gmail(local: str, domain: str) -> tuple[str, str]:
    """Apply Gmail's dot-insensitive, plus-addressing equivalence.

    ``googlemail.com`` is Gmail's legacy domain — both map to ``gmail.com``.
    A ``+tag`` suffix and any ``.`` in the local part are routing artifacts;
    Gmail delivers all variants to the same mailbox.
    """
    if domain not in _GMAIL_DOMAINS:
        return local, domain
    canonical_local = local.split("+", 1)[0].replace(".", "")
    if not canonical_local:
        return local, domain
    return canonical_local, "gmail.com"


def normalize_email(raw: str) -> tuple[str | None, QualityFlag]:
    """Return ``(normalized_email, quality_flag)`` for a raw email string.

    Normalization: lowercase, strip whitespace, canonicalize Gmail/Googlemail
    addresses (dot-insensitive local part, ``+tag`` stripped, domain folded to
    ``gmail.com``).
    """
    stripped = raw.strip().lower()
    if not stripped:
        return None, QualityFlag.INVALID_FORMAT

    if stripped in _PLACEHOLDER_PATTERNS:
        return None, QualityFlag.PLACEHOLDER_VALUE

    if not _EMAIL_RE.match(stripped):
        return None, QualityFlag.INVALID_FORMAT

    local, _, domain = stripped.rpartition("@")
    local, domain = _canonicalize_gmail(local, domain)
    return f"{local}@{domain}", QualityFlag.VALID
