"""Shared loyalty-points helpers for phppos identity connectors (Eko, SpeedZone).

Both Eko and SpeedZone expose the same ``phppos_customers`` loyalty columns
(``points``, ``disable_loyalty``, ``current_spend_for_points``,
``current_sales_for_discount``). The live DB returns ``Decimal``/``int``; the
dump path (which round-trips columns as text) returns ``str``. These helpers
normalize both shapes into JSON-safe primitives so the loyalty block written
into the identity ``raw_payload`` is consistent across live and dump paths.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from src.models import JsonValue


def to_int_or_none(value: object) -> int | None:
    """Coerce to int, preserving None. Handles Decimal (live) and numeric str (dump).

    ``int("300.000")`` raises, so fall back through float for decimal strings.
    Narrowing by ``isinstance`` keeps the calls mypy-clean under strict without
    ``type: ignore`` (whose error code differs between ``int`` and ``float``).
    """
    if value is None:
        return None
    if isinstance(value, bool | int | float | Decimal):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(float(value))
            except ValueError:
                return None
    return None


def to_float_or_none(value: object) -> float | None:
    """Coerce to float, preserving None. Handles Decimal (live) and numeric str (dump)."""
    if value is None:
        return None
    if isinstance(value, bool | int | float | Decimal):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def to_bool_or_none(value: object) -> bool | None:
    """Coerce a tinyint(1) loyalty flag to bool, preserving None.

    The source stores ``disable_loyalty`` as 0/1; a Decimal/int/str all coerce
    via ``int`` first so "0"/0/Decimal(0) all map to False.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return bool(int(value))
    if isinstance(value, str):
        try:
            return bool(int(value))
        except ValueError:
            return None
    return None


def loyalty_block_from_row(row: Any) -> dict[str, JsonValue]:
    """Build the loyalty-points balance block for the identity SourceRecord raw_payload.

    Reads defensively via ``getattr`` so rows whose SELECT omitted the loyalty
    columns (older phppos DBs) yield None rather than raising.
    """
    return {
        "points": to_int_or_none(getattr(row, "points", None)),
        "disable_loyalty": to_bool_or_none(getattr(row, "disable_loyalty", None)),
        "current_spend_for_points": to_float_or_none(
            getattr(row, "current_spend_for_points", None)
        ),
        "current_sales_for_discount": to_float_or_none(
            getattr(row, "current_sales_for_discount", None)
        ),
    }
