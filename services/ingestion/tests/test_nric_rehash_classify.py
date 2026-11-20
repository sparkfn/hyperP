"""Unit coverage for NRIC identifier classification rules."""

from __future__ import annotations

import secrets
import string
from collections.abc import Callable

import pytest
from _nric_rehash_plan_fakes import KEY_VERSION, _row, _valid_nric
from src.normalizers.nric import (
    is_nric_digest,
    mask_nric,
    nric_checksum_valid,
    nric_digest,
)
from src.nric_rehash_classify import (
    NricClassification,
    NricIdentifierRow,
    classify_nric_identifier,
)


def _invalid_checksum(canonical: str) -> str:
    for letter in string.ascii_uppercase:
        candidate = f"{canonical[:-1]}{letter}"
        if candidate != canonical and not nric_checksum_valid(candidate):
            return candidate
    raise AssertionError("could not synthesize an invalid checksum")


def _complete_digest_row(identifier_id: str, digest: str) -> NricIdentifierRow:
    return _row(
        identifier_id,
        digest,
        hashed_value=digest,
        nric_masked=mask_nric(_valid_nric()),
        checksum_valid=True,
        key_version=KEY_VERSION,
        encrypted=secrets.token_urlsafe(24),
    )


def test_classification_converts_canonicalized_variants_to_same_digest() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    variants = (canonical, f"xxx{canonical}", canonical.lower())
    classifications = [
        classify_nric_identifier(_row(f"id-{index}", value), key, KEY_VERSION)
        for index, value in enumerate(variants)
    ]
    assert all(result.category == "convertible" for result in classifications)
    assert {result.digest for result in classifications} == {nric_digest(canonical, key)}
    assert all(result.canonical == canonical for result in classifications)
    assert all(result.masked == mask_nric(canonical) for result in classifications)


def test_checksum_invalid_shape_is_still_convertible() -> None:
    classification = classify_nric_identifier(
        _row("opaque-id", _invalid_checksum(_valid_nric())), secrets.token_hex(32), KEY_VERSION
    )
    assert classification.category == "convertible"
    assert classification.checksum_valid is False
    assert classification.digest is not None


def test_sensitive_values_are_excluded_from_row_and_classification_repr() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    digest = nric_digest(canonical, key)
    masked = mask_nric(canonical)
    ciphertext = secrets.token_urlsafe(24)
    row = NricIdentifierRow(
        identifier_id="opaque-id",
        identifier_scope="global",
        normalized_value=canonical,
        hashed_value=digest,
        nric_masked=masked,
        checksum_valid=nric_checksum_valid(canonical),
        nric_key_version=KEY_VERSION,
        nric_encrypted=ciphertext,
    )
    classification = classify_nric_identifier(row, key, KEY_VERSION)
    representation = f"{row!r}{classification!r}"

    assert isinstance(classification, NricClassification)
    assert classification.category == "convertible"
    for sensitive in (canonical, digest, masked, ciphertext):
        assert sensitive not in representation


def test_digest_metadata_gaps_include_missing_mismatch_and_wrong_version() -> None:
    digest = nric_digest(_valid_nric(), secrets.token_hex(32))
    complete = _complete_digest_row("opaque-complete", digest)
    missing = _row("opaque-missing", digest, hashed_value=digest)
    wrong_version = _row(
        "opaque-version",
        digest,
        hashed_value=digest,
        nric_masked=mask_nric(_valid_nric()),
        checksum_valid=True,
        key_version="older-version",
        encrypted=secrets.token_urlsafe(24),
    )
    mismatch = _row(
        "opaque-hash-mismatch",
        digest,
        hashed_value=nric_digest(_valid_nric(), secrets.token_hex(32)),
        nric_masked=mask_nric(_valid_nric()),
        checksum_valid=True,
        key_version=KEY_VERSION,
        encrypted=secrets.token_urlsafe(24),
    )
    results = [
        classify_nric_identifier(row, secrets.token_hex(32), KEY_VERSION)
        for row in (complete, missing, wrong_version, mismatch)
    ]
    assert all(result.category == "already_digest" for result in results)
    assert [result.digest_metadata_gap for result in results] == [False, True, True, True]


@pytest.mark.parametrize(
    ("value_factory", "expected_reason"),
    [
        (lambda value: mask_nric(value), "masked"),
        (lambda _value: None, "empty"),
        (lambda _value: 123, "empty"),
        (lambda _value: "xxxx", "empty"),
        (lambda _value: "not-an-identifier", "unknown_format"),
    ],
)
def test_unprocessable_classification_reasons(
    value_factory: Callable[[str], object], expected_reason: str
) -> None:
    row = _row("opaque-id", value_factory(_valid_nric()))
    classified = classify_nric_identifier(row, secrets.token_hex(32), KEY_VERSION)
    assert classified.category == "unprocessable"
    assert classified.reason == expected_reason


def test_digest_lookalikes_are_not_accepted_as_digests() -> None:
    digest = nric_digest(_valid_nric(), secrets.token_hex(32))
    uppercase_hex = f"A{digest[1:]}"
    assert is_nric_digest(digest)
    lookalikes = (uppercase_hex, digest[:-1], f"{digest}a")
    for index, value in enumerate(lookalikes):
        classified = classify_nric_identifier(
            _row(f"opaque-{index}", value), secrets.token_hex(32), KEY_VERSION
        )
        assert classified.category == "unprocessable"
        assert classified.reason == "unknown_format"


def test_missing_id_unexpected_scope_and_null_scope() -> None:
    key = secrets.token_hex(32)
    canonical = _valid_nric()
    missing_id = classify_nric_identifier(_row(None, canonical), key, KEY_VERSION)
    unexpected_scope = classify_nric_identifier(
        _row("opaque-tenant", canonical, scope="tenant-private"), key, KEY_VERSION
    )
    null_scope = classify_nric_identifier(
        _row("opaque-null-scope", canonical, scope=None), key, KEY_VERSION
    )
    assert missing_id.blocker == "missing_id"
    assert unexpected_scope.blocker == "unexpected_scope"
    assert null_scope.category == "convertible"
