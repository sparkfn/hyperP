"""Pure unit tests for the C3 identity-link foundation helpers."""

from __future__ import annotations

import hashlib

from src.identity_links import (
    LOWEST_TRUST_TIER,
    AssertionEvidence,
    IdentifierKey,
    aggregate_link,
    link_id,
    materializes,
    sorted_lock_rows,
)


def _row(
    *,
    source_record_pk: str = "source-1",
    identifier_type: str = "phone",
    identifier_scope: str = "global",
    normalized_value: str = "+6591234567",
    quality_flag: str | None = "valid",
    is_verified: bool | None = True,
    trust_tier: str | None = "tier_3",
    observed_at: str | None = "2026-01-01T00:00:00Z",
) -> AssertionEvidence:
    return AssertionEvidence(
        source_record_pk=source_record_pk,
        identifier_type=identifier_type,
        identifier_scope=identifier_scope,
        normalized_value=normalized_value,
        quality_flag=quality_flag,
        is_verified=is_verified,
        trust_tier=trust_tier,
        observed_at=observed_at,
    )


def _key(
    identifier_type: str,
    identifier_scope: str,
    normalized_value: str,
) -> IdentifierKey:
    return IdentifierKey(
        identifier_type=identifier_type,
        identifier_scope=identifier_scope,
        normalized_value=normalized_value,
    )


def test_link_id_matches_known_sha1_vectors() -> None:
    assert link_id("person-1", "identifier-1") == "ff6eff5ee53cabdb4fdb3fe4d961d6fca3361776"
    assert link_id("profile-1", "identifier-2") == "d1c1177e37c0eb866638b4337f8c9bb6b67fe846"
    assert link_id("source-record-1", "identifier-3") == "2783c901d7cb1f1abc6938f0242fc4434ed692e6"
    assert link_id("person-1", "identifier-1") == hashlib.sha1(b"person-1|identifier-1").hexdigest()


def test_aggregate_collects_counts_tiers_timestamps_and_provisionals() -> None:
    aggregate = aggregate_link(
        [
            _row(trust_tier="tier_1", observed_at="2026-01-01T00:00:00Z"),
            _row(
                source_record_pk="source-2",
                trust_tier="tier_2",
                observed_at="2026-02-01T00:00:00Z",
            ),
            _row(trust_tier="tier_3", is_verified=False, observed_at="2026-03-01T00:00:00Z"),
        ]
    )

    assert aggregate.assertion_count == 3
    assert aggregate.source_count == 2
    assert aggregate.verified_count == 2
    assert aggregate.max_tier == "tier_1"
    assert aggregate.first_seen_at == "2026-01-01T00:00:00Z"
    assert aggregate.last_seen_at == "2026-03-01T00:00:00Z"
    assert aggregate.last_confirmed_at == "2026-03-01T00:00:00Z"
    assert aggregate.score == 0.0
    assert aggregate.exclusivity == 0.0
    assert aggregate.rank_score == 0.0
    assert aggregate.golden_rank is None
    assert aggregate.basis == "ASSERTS"
    assert aggregate.status == "active"
    assert aggregate.policy_version == "c3-provisional"


def test_aggregate_without_observations_keeps_null_boundaries() -> None:
    aggregate = aggregate_link([_row(observed_at=None)])

    assert aggregate.assertion_count == 1
    assert aggregate.first_seen_at is None
    assert aggregate.last_seen_at is None
    assert aggregate.last_confirmed_at is None


def test_assertion_missing_quality_flag_contributes_no_verified_count() -> None:
    aggregate = aggregate_link([_row(quality_flag=None, is_verified=True, trust_tier="tier_1")])

    assert aggregate.verified_count == 0
    assert aggregate.max_tier == LOWEST_TRUST_TIER


def test_assertion_missing_is_verified_falls_back_to_lowest_tier() -> None:
    aggregate = aggregate_link([_row(quality_flag="valid", is_verified=None, trust_tier="tier_1")])

    assert aggregate.verified_count == 0
    assert aggregate.max_tier == LOWEST_TRUST_TIER


def test_assertion_missing_metadata_never_raises_the_max_tier() -> None:
    aggregate = aggregate_link(
        [
            _row(quality_flag="valid", is_verified=True, trust_tier="tier_3"),
            _row(quality_flag=None, is_verified=True, trust_tier="tier_4"),
        ]
    )

    assert aggregate.max_tier == "tier_3"


def test_single_record_with_one_key_does_not_materialize() -> None:
    assert materializes([_row()]) is False


def test_two_records_sharing_an_eligible_key_materialize() -> None:
    evidence = [_row(source_record_pk="source-1"), _row(source_record_pk="source-2")]

    assert materializes(evidence) is True


def test_two_records_without_a_shared_key_do_not_materialize() -> None:
    evidence = [
        _row(source_record_pk="source-1", normalized_value="+6591234567"),
        _row(source_record_pk="source-2", normalized_value="+6598765432"),
    ]

    assert materializes(evidence) is False


def test_one_record_with_two_independent_eligible_keys_materializes() -> None:
    evidence = [
        _row(identifier_type="phone", normalized_value="+6591234567"),
        _row(identifier_type="email", normalized_value="user@example.com"),
    ]

    assert materializes(evidence) is True


def test_one_record_key_missing_metadata_is_not_independent() -> None:
    evidence = [
        _row(identifier_type="phone", normalized_value="+6591234567"),
        _row(identifier_type="email", normalized_value="user@example.com", is_verified=None),
    ]

    assert materializes(evidence) is False


def test_unusable_quality_does_not_satisfy_the_threshold() -> None:
    evidence = [
        _row(source_record_pk="source-1", quality_flag="invalid_format"),
        _row(source_record_pk="source-2", quality_flag="valid"),
    ]

    assert materializes(evidence) is False


def test_sorted_lock_rows_use_type_scope_value_order() -> None:
    rows = [
        _key("phone", "global", "+6591234567"),
        _key("email", "global", "user@example.com"),
        _key("phone", "source:eko", "+6591234567"),
        _key("phone", "global", "+6598765432"),
    ]

    ordered = sorted_lock_rows(rows)

    assert [
        (row["identifier_type"], row["identifier_scope"], row["normalized_value"])
        for row in ordered
    ] == [
        ("email", "global", "user@example.com"),
        ("phone", "global", "+6591234567"),
        ("phone", "global", "+6598765432"),
        ("phone", "source:eko", "+6591234567"),
    ]
    # Input order is left untouched.
    assert rows[0] == _key("phone", "global", "+6591234567")
