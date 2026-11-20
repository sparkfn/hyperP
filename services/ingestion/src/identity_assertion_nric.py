"""Key legacy NRIC assertion rows before replaying immutable payloads."""

from __future__ import annotations

from collections.abc import Sequence

from src.config import get_settings
from src.models import JsonValue
from src.normalizers.nric import is_nric_digest
from src.nric_rehash_classify import NricIdentifierRow, classify_nric_identifier


class LegacyNricReplayError(RuntimeError):
    """A replay row cannot be safely mapped to the configured NRIC digest."""

    def __init__(self) -> None:
        super().__init__("legacy NRIC assertion replay refused")


def keyed_identifier_rows(
    rows: Sequence[dict[str, JsonValue]],
) -> list[dict[str, JsonValue]]:
    """Copy rows and key recoverable legacy NRIC values without changing assertion identity."""
    keyed_rows: list[dict[str, JsonValue]] = []
    key_configuration: tuple[str, str] | None = None
    for row in rows:
        copied = dict(row)
        if row.get("identifier_type") != "nric":
            keyed_rows.append(copied)
            continue
        value: JsonValue | None = row.get("normalized_value")
        if isinstance(value, str) and is_nric_digest(value):
            keyed_rows.append(copied)
            continue
        if key_configuration is None:
            key_configuration = _key_configuration()
        key, key_version = key_configuration
        classification = classify_nric_identifier(_classifier_row(row), key, key_version)
        if classification.blocker is not None:
            raise LegacyNricReplayError
        if classification.category == "unprocessable":
            keyed_rows.append(copied)
            continue
        digest = classification.digest
        if classification.category != "convertible" or not key or not digest:
            raise LegacyNricReplayError
        copied["normalized_value"] = digest
        copied["hashed_value"] = digest
        copied.pop("target_id", None)
        keyed_rows.append(copied)
    return keyed_rows


def _key_configuration() -> tuple[str, str]:
    try:
        settings = get_settings()
    except Exception:
        raise LegacyNricReplayError from None
    return settings.nric_hmac_key, settings.nric_hmac_key_version


def _classifier_row(row: dict[str, JsonValue]) -> NricIdentifierRow:
    assertion_id = row.get("assertion_id")
    scope = row.get("identifier_scope")
    value = row.get("normalized_value")
    return NricIdentifierRow(
        identifier_id=assertion_id if isinstance(assertion_id, str) else None,
        identifier_scope=scope if scope is None or isinstance(scope, str) else "",
        normalized_value=value if isinstance(value, str) else None,
        hashed_value=None,
        nric_masked=None,
        checksum_valid=None,
        nric_key_version=None,
        nric_encrypted=None,
    )
