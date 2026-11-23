"""Generated data builders shared by NRIC apply unit tests."""

from __future__ import annotations

import json
import secrets
import string
from typing import cast

from src.config import Settings
from src.normalizers.nric import nric_checksum_valid

_KEY_VERSION = "apply-test-version"


def _canonical(exclude: set[str] | None = None) -> str:
    excluded = exclude or set()
    while True:
        digits = f"{secrets.randbelow(10_000_000):07d}"
        if len(set(digits)) == 1:
            continue
        for letter in string.ascii_uppercase:
            candidate = f"S{digits}{letter}"
            if candidate not in excluded and nric_checksum_valid(candidate):
                return candidate


def _row(
    identifier_id: str | None, value: object, scope: str | None = "global"
) -> dict[str, object]:
    return {
        "identifier_id": identifier_id,
        "identifier_type": "nric",
        "identifier_scope": scope,
        "normalized_value": value,
        "hashed_value": None,
        "nric_masked": None,
        "checksum_valid": None,
        "nric_key_version": None,
        "nric_encrypted": None,
    }


def _settings(key: str, key_version: str = _KEY_VERSION) -> Settings:
    return Settings.model_construct(
        nric_hmac_key=key,
        nric_hmac_key_version=key_version,
        neo4j_password="",
    )


def _report(output: str) -> dict[str, object]:
    decoded: object = json.loads(output)
    assert isinstance(decoded, dict)
    return cast(dict[str, object], decoded)
