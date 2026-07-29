"""Closed source-value types needed by the API profile-analysis runtime."""

from __future__ import annotations

from enum import StrEnum

from pydantic.types import JsonValue

__all__ = ["JsonValue", "QualityFlag", "RecordType"]


class QualityFlag(StrEnum):
    VALID = "valid"
    INVALID_FORMAT = "invalid_format"
    PLACEHOLDER_VALUE = "placeholder_value"
    SHARED_SUSPECTED = "shared_suspected"
    STALE = "stale"
    SOURCE_UNTRUSTED = "source_untrusted"
    PARTIAL_PARSE = "partial_parse"


class RecordType(StrEnum):
    IDENTITY = "identity"
    BANKRUPTCY = "bankruptcy"
    RENTAL_FLAT = "rental_flat"
    RELATIONSHIP = "relationship"
    CONVERSATION = "conversation"
    SALES = "sales"
