"""Exact conversion for optional PHPPOS order loyalty-point projections."""

from __future__ import annotations

import hashlib
import logging
import math
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Literal

IntegralConversionErrorCode = Literal[
    "boolean",
    "malformed",
    "non_finite",
    "non_integral",
    "out_of_range",
    "string_too_long",
    "unsafe_float",
    "unsupported_type",
]

_MIN_INT64 = -(2**63)
_MAX_INT64 = 2**63 - 1
_MIN_INT64_DECIMAL = Decimal(_MIN_INT64)
_MAX_INT64_DECIMAL = Decimal(_MAX_INT64)
_MAX_STRING_LENGTH = 128
_MAX_SAFE_FLOAT_INTEGER = 2**53 - 1
_WARNING_CACHE_LIMIT = 1024

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegralConversion:
    """A non-throwing exact-integer conversion result."""

    value: int | None
    error_code: IntegralConversionErrorCode | None


def convert_integral(value: object) -> IntegralConversion:
    """Convert an untrusted optional value to a Neo4j-safe exact integer."""
    if value is None:
        return IntegralConversion(None, None)
    if isinstance(value, bool):
        return _error("boolean")
    if isinstance(value, int):
        return _from_int(value)
    if isinstance(value, Decimal):
        return _from_decimal(value)
    if isinstance(value, str):
        if len(value) > _MAX_STRING_LENGTH:
            return _error("string_too_long")
        stripped = value.strip()
        if not stripped:
            return _error("malformed")
        try:
            return _from_decimal(Decimal(stripped))
        except InvalidOperation:
            return _error("malformed")
    if isinstance(value, float):
        if not math.isfinite(value):
            return _error("non_finite")
        if not value.is_integer():
            return _error("non_integral")
        if abs(value) > _MAX_SAFE_FLOAT_INTEGER:
            return _error("unsafe_float")
        return _from_int(int(value))
    return _error("unsupported_type")


def loyalty_order_digest(source: str, source_order_id: str) -> str:
    """Return the stable privacy-safe digest used in loyalty diagnostics."""
    return hashlib.sha256(f"{source}\0{source_order_id}".encode()).hexdigest()


_warning_cache: OrderedDict[tuple[str, str, str, str, str], None] = OrderedDict()
_warning_cache_lock = Lock()


def warn_invalid_loyalty_once(
    *,
    source: str | None,
    source_order_id: str | None,
    field: Literal["points_used", "points_gained"],
    conversion: IntegralConversion,
    raw_value: object,
) -> None:
    """Log one bounded privacy-safe warning for an invalid loyalty field."""
    reason = conversion.error_code
    if reason is None:
        return
    normalized_source = (
        source.strip()[:100] if isinstance(source, str) and source.strip() else "unknown"
    )
    normalized_order_id = source_order_id if isinstance(source_order_id, str) else ""
    input_type = type(raw_value).__name__[:80]
    digest = loyalty_order_digest(normalized_source, normalized_order_id)
    key = (normalized_source, digest, field, reason, input_type)
    with _warning_cache_lock:
        if key in _warning_cache:
            return
        _warning_cache[key] = None
        if len(_warning_cache) > _WARNING_CACHE_LIMIT:
            _warning_cache.popitem(last=False)
    logger.warning(
        "event=loyalty_points_conversion_failed source=%s field=%s reason=%s "
        "input_type=%s order_digest=%s",
        normalized_source,
        field,
        reason,
        input_type,
        digest,
    )


def normalize_loyalty_field(
    value: object,
    *,
    source: str,
    source_order_id: str,
    field: Literal["points_used", "points_gained"],
) -> int | None:
    """Convert one loyalty field and emit a privacy-safe diagnostic on failure."""
    conversion = convert_integral(value)
    warn_invalid_loyalty_once(
        source=source,
        source_order_id=source_order_id,
        field=field,
        conversion=conversion,
        raw_value=value,
    )
    return conversion.value


def _from_decimal(value: Decimal) -> IntegralConversion:
    if not value.is_finite():
        return _error("non_finite")
    if value != value.to_integral_value():
        return _error("non_integral")
    if value < _MIN_INT64_DECIMAL or value > _MAX_INT64_DECIMAL:
        return _error("out_of_range")
    return _from_int(int(value))


def _from_int(value: int) -> IntegralConversion:
    if value < _MIN_INT64 or value > _MAX_INT64:
        return _error("out_of_range")
    return IntegralConversion(value, None)


def _error(code: IntegralConversionErrorCode) -> IntegralConversion:
    return IntegralConversion(None, code)
