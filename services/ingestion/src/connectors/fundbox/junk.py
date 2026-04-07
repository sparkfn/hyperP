"""Sentinel/junk identifier filter.

The Fundbox source DB stores literal strings like ``"Empty"``, ``"Nil"``, or
``"NA"`` in nullable identifier columns instead of NULL. If forwarded to the
graph these become hub nodes that link dozens of unrelated persons together
and cause candidate-generation explosions during matching. The filter is
intentionally aggressive for free-text identifier types only — NRIC, phone,
and email are all parsed by stricter normalizers downstream and don't go
through this check.
"""

from __future__ import annotations

_JUNK_IDENTIFIER_VALUES: frozenset[str] = frozenset(
    {
        "",
        "0",
        "-",
        "--",
        "n/a",
        "na",
        "nil",
        "null",
        "none",
        "empty",
        "unknown",
        "test",
        "facebook",
        "fb",
    }
)

# Identifier types that get junk-filtering applied. Other types (nric, phone,
# email) have downstream normalizers that handle invalid values explicitly.
JUNK_FILTERED_TYPES: frozenset[str] = frozenset({"device_id"})
JUNK_FILTERED_TYPE_PREFIXES: tuple[str, ...] = ("social:",)


def is_junk_identifier(value: str) -> bool:
    v = value.strip().lower()
    if not v or len(v) < 4:
        return True
    return v in _JUNK_IDENTIFIER_VALUES


def should_filter(id_type: str) -> bool:
    if id_type in JUNK_FILTERED_TYPES:
        return True
    return any(id_type.startswith(p) for p in JUNK_FILTERED_TYPE_PREFIXES)
