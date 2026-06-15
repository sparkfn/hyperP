"""NRIC / FIN government-identifier normalization.

The canonical normalized value is the upper-cased identifier with junk ``x`` /
``X`` edge markers removed. No checksum validation is performed here — the
normalized value is the key used for deterministic government-ID matching, and
upstream data is not guaranteed to be checksum-clean.
"""

from __future__ import annotations

from src.models import QualityFlag
from src.normalizers.clean import strip_leading_x_markers


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
