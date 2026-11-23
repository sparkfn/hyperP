"""Privacy-safe normalization for pending-review NRIC activation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import string
from typing import cast
from unittest.mock import patch

import pytest
from neo4j import AsyncManagedTransaction
from src.graph.queries.review import ACTIVATE_PENDING_REVIEW_RECORD
from src.nric import (
    LegacyNricActivationError,
    keyed_pending_identifiers,
    mask_nric,
)
from src.repositories.neo4j.review import (
    _pending_projection_params,
    _pending_record_merge_tx,
)


def _canonical() -> str:
    return (
        secrets.choice("STFG")
        + "".join(secrets.choice(string.digits) for _ in range(7))
        + secrets.choice(string.ascii_uppercase)
    )


def _key() -> str:
    return secrets.token_hex(32)


def _independent_digest(canonical: str, key: str) -> str:
    return hmac.new(key.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _safe_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        pytest.fail(f"{label} mismatch")


def _item(
    identifier_type: str,
    value: str,
    *,
    quality_flag: str = "valid",
    is_verified: bool = True,
) -> dict[str, object]:
    return {
        "identifier_type": identifier_type,
        "normalized_value": value,
        "quality_flag": quality_flag,
        "is_verified": is_verified,
    }


def _payload(value: str, identifier_type: str = "nric") -> str:
    return json.dumps({"identifiers": [_item(identifier_type, value)]})


def _projection(
    payload: dict[str, object], *, source_record_pk: str = "generated-record"
) -> tuple[list[dict[str, object]], ...]:
    return _pending_projection_params(
        json.dumps(payload),
        source_system_key="generated-source",
        source_record_id="generated-source-record",
        source_record_pk=source_record_pk,
    )


def test_legacy_forms_key_to_one_digest_without_mutation() -> None:
    canonical = _canonical()
    key = _key()
    variants = (canonical, canonical.lower(), f"xx{canonical}", f"  {canonical} ")
    items = [
        {
            **_item("nric", value, is_verified=index % 2 == 0),
            "identifier_scope": "legacy",
            "source_instance_id": f"instance-{index}",
        }
        for index, value in enumerate(variants)
    ]
    original = [dict(item) for item in items]
    digest = _independent_digest(canonical, key)

    with patch("src.nric.config.nric_hmac_key", key):
        mapped = keyed_pending_identifiers(items)

    expected = [
        {**item, "normalized_value": digest, "identifier_scope": "global"} for item in items
    ]
    _safe_equal(mapped, expected, "canonical variants and metadata")
    _safe_equal([dict(item) for item in items], original, "input immutability")


def test_digest_masked_and_invalid_shape_do_not_require_a_key() -> None:
    canonical = _canonical()
    generated_key = _key()
    digest = _independent_digest(canonical, generated_key)
    items = [
        _item("nric", digest),
        _item("nric", mask_nric(canonical)),
        _item("nric", f"unknown-{secrets.token_hex(4)}"),
    ]

    with (
        patch("src.nric.config.nric_hmac_key", ""),
        patch(
            "src.nric.nric_digest", side_effect=AssertionError("digest calculation was unexpected")
        ),
    ):
        mapped = keyed_pending_identifiers(items)

    expected = [{**item, "identifier_scope": "global"} for item in items]
    _safe_equal(mapped, expected, "no-key pass-through classifications")


def test_kelvin_sign_check_letter_is_shape_invalid_like_the_rehash() -> None:
    canonical = _canonical()
    item = _item("nric", f"{canonical[:-1]}K")
    with (
        patch("src.nric.config.nric_hmac_key", ""),
        patch("src.nric.nric_digest", side_effect=AssertionError("digest was unexpected")),
    ):
        mapped = keyed_pending_identifiers([item])

    expected = [{**item, "identifier_scope": "global"}]
    _safe_equal(mapped, expected, "case-sensitive shape pass-through")


def test_convertible_without_key_fails_with_fixed_value_free_error() -> None:
    canonical = _canonical()
    key = _key()
    with patch("src.nric.config.nric_hmac_key", ""):
        with pytest.raises(LegacyNricActivationError) as error:
            keyed_pending_identifiers([_item("nric", canonical)])

    _safe_equal(str(error.value), "legacy NRIC activation refused", "fixed error")
    if canonical in str(error.value) or key in str(error.value):
        pytest.fail("activation error exposed a generated value")
    assert isinstance(error.value, ValueError)


def test_non_nric_items_are_exact_copies_in_mixed_order() -> None:
    items = [
        _item("email", f"person-{secrets.token_hex(4)}@example.invalid"),
        _item("phone", f"phone-{secrets.token_hex(4)}"),
        _item("external_customer_id", f"crm-{secrets.token_hex(4)}"),
        _item("nric", f"unknown-{secrets.token_hex(4)}"),
    ]

    mapped = keyed_pending_identifiers(items)
    expected = [*items[:3], {**items[3], "identifier_scope": "global"}]

    _safe_equal(mapped, expected, "mixed item order and values")
    if any(output is original for output, original in zip(mapped, items, strict=True)):
        pytest.fail("mapping reused an input dictionary")
    for output in mapped[:3]:
        if "identifier_scope" in output:
            pytest.fail("non-NRIC item gained a scope")


def test_duplicate_variants_remain_distinct_and_mapping_is_idempotent() -> None:
    canonical = _canonical()
    key = _key()
    expected_digest = _independent_digest(canonical, key)
    items = [_item("nric", canonical), _item("nric", canonical.lower())]
    with patch("src.nric.config.nric_hmac_key", key):
        mapped = keyed_pending_identifiers(items)
        repeated = keyed_pending_identifiers(mapped)

    _safe_equal(
        [item["normalized_value"] for item in mapped],
        [expected_digest, expected_digest],
        "duplicate variant targets",
    )
    _safe_equal(repeated, mapped, "idempotent mapping")
    assert len(mapped) == 2


def test_projection_maps_after_quality_filter_and_fails_closed_without_key() -> None:
    canonical = _canonical()
    key = _key()
    valid_item = _item("nric", canonical, is_verified=False)
    payload: dict[str, object] = {
        "identifiers": [
            valid_item,
            _item("nric", _canonical(), quality_flag="invalid_format"),
            _item("nric", _canonical(), quality_flag="placeholder_value"),
        ]
    }
    expected_digest = _independent_digest(canonical, key)
    with patch("src.nric.config.nric_hmac_key", key):
        projected = _projection(payload)

    _safe_equal(
        projected[0],
        [{**valid_item, "normalized_value": expected_digest, "identifier_scope": "global"}],
        "projected legacy identifier",
    )
    if canonical in repr(projected):
        pytest.fail("projection repr exposed a legacy NRIC")

    filtered_payload: dict[str, object] = {
        "identifiers": [
            _item("nric", _canonical(), quality_flag="invalid_format"),
            _item("nric", _canonical(), quality_flag="placeholder_value"),
        ]
    }
    with patch("src.nric.config.nric_hmac_key", ""):
        filtered = _projection(filtered_payload)
    _safe_equal(filtered[0], [], "filtered identifiers bypass key requirement")

    with patch("src.nric.config.nric_hmac_key", ""):
        with pytest.raises(LegacyNricActivationError):
            _projection({"identifiers": [_item("nric", _canonical())]})


class _UnexpectedTransaction:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, _query: str, **_parameters: object) -> object:
        self.calls += 1
        raise AssertionError("transaction should not be used")


@pytest.mark.asyncio
async def test_projection_error_fails_closed_before_transaction() -> None:
    tx = _UnexpectedTransaction()
    pending_record: dict[str, object] = {
        "proposed_person_id": "generated-person",
        "review_candidate_person_ids": [],
        "pending_source_record_pk": "generated-pending-record",
        "source_system_key": "generated-source",
        "source_record_id": "generated-source-record",
        "observed_at": "2026-11-20T00:00:00+00:00",
        "normalized_payload": json.dumps({"identifiers": [_item("nric", _canonical())]}),
        "expected_active_source_record_pk": None,
    }
    with patch("src.nric.config.nric_hmac_key", ""):
        result = await _pending_record_merge_tx(
            cast(AsyncManagedTransaction, tx),
            "generated-review-case",
            pending_record,
            None,
            "closed",
            "merge",
            None,
            "generated-actor",
            None,
        )

    _safe_equal(result, {"merge_not_applicable": True}, "fail-closed result")
    assert tx.calls == 0


def test_activation_query_sets_scope_and_preserves_non_nric_null_behavior() -> None:
    assert "id.identifier_scope = ident.identifier_scope" in ACTIVATE_PENDING_REVIEW_RECORD
