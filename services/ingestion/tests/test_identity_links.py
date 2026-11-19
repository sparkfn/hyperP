"""Pure unit tests for the C3 identity-link foundation helpers."""

from __future__ import annotations

import hashlib

from src.identity_link_member_evidence import MemberEvidence, cross_member_contradiction
from src.identity_link_weights import LINK_POLICY_VERSION
from src.identity_links import (
    LOWEST_TRUST_TIER,
    AssertionEvidence,
    IdentifierKey,
    ParticipantEvidence,
    aggregate_link,
    build_link_rows,
    identifier_key_row,
    identifier_key_tuple,
    link_id,
    materializes,
    parse_field_trust,
    sorted_lock_rows,
)
from src.models import NormalizedIdentifier, QualityFlag, RecordType


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


def test_aggregate_collects_counts_tiers_timestamps_and_weights() -> None:
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
    # source-1 dedupes to max(1.0, 0.6) = 1.0, source-2 is 0.9775:
    # noisy-OR is 1.0, exclusivity defaults to 1.0, decay anchors at the
    # link's own last observation.
    assert aggregate.score == 1.0
    assert aggregate.exclusivity == 1.0
    assert aggregate.rank_score == 1.0
    assert aggregate.golden_rank is None
    assert aggregate.basis == "ASSERTS"
    assert aggregate.status == "active"
    assert aggregate.policy_version == LINK_POLICY_VERSION


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


def _participant_row(
    *,
    identifier_id: str = "identifier-1",
    source_record_pk: str = "source-1",
    identifier_type: str = "phone",
    normalized_value: str = "+6591234567",
) -> ParticipantEvidence:
    return ParticipantEvidence(
        source_record_pk=source_record_pk,
        identifier_id=identifier_id,
        identifier_type=identifier_type,
        identifier_scope="global",
        normalized_value=normalized_value,
        quality_flag="valid",
        is_verified=True,
        trust_tier="tier_2",
        observed_at="2026-01-01T00:00:00Z",
    )


def test_build_link_rows_groups_evidence_per_identifier() -> None:
    rows = build_link_rows(
        "profile-1",
        [
            _participant_row(source_record_pk="source-1"),
            _participant_row(source_record_pk="source-2"),
            _participant_row(
                identifier_id="identifier-2",
                identifier_type="email",
                normalized_value="user@example.com",
            ),
        ],
    )

    # Groups are emitted in canonical key order (identifier_type, scope, value),
    # the same order used for Identifier lock acquisition: email before phone.
    assert [row["link_id"] for row in rows] == [
        link_id("profile-1", "identifier-2"),
        link_id("profile-1", "identifier-1"),
    ]
    assert rows[0]["identifier_id"] == "identifier-2"
    assert rows[0]["assertion_count"] == 1
    assert rows[0]["max_tier"] == "tier_2"
    assert rows[0]["score"] == 0.9775
    assert rows[0]["exclusivity"] == 1.0
    assert rows[0]["rank_score"] == 0.9775
    assert rows[0]["golden_rank"] is None
    assert rows[0]["policy_version"] == LINK_POLICY_VERSION
    assert rows[1]["identifier_id"] == "identifier-1"
    assert rows[1]["assertion_count"] == 2
    assert rows[1]["source_count"] == 2
    assert rows[1]["score"] == 0.999494
    assert rows[1]["exclusivity"] == 1.0
    assert rows[1]["rank_score"] == 0.999494
    assert rows[1]["policy_version"] == LINK_POLICY_VERSION


def test_parse_field_trust_handles_json_maps_and_malformed_values() -> None:
    assert parse_field_trust('{"nric": "tier_4", "phone": "tier_1"}') == {
        "nric": "tier_4",
        "phone": "tier_1",
    }
    assert parse_field_trust({"phone": "tier_1"}) == {"phone": "tier_1"}
    assert parse_field_trust("{not-json") is None
    assert parse_field_trust(["phone"]) is None
    assert parse_field_trust(None) is None


def test_identifier_key_tuple_round_trips() -> None:
    key = _key("phone", "global", "+6591234567")

    assert identifier_key_row(identifier_key_tuple(key)) == key


def _evidence(pk: str, *, dobs: tuple[str, ...]) -> MemberEvidence:
    return MemberEvidence(source_record_pk=pk, full_names=(), dobs=dobs, nric_entries=())


def test_durable_holder_values_are_compared_but_never_self_checked() -> None:
    identifier = NormalizedIdentifier(
        identifier_type="nric",
        normalized_value="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        is_verified=True,
        quality_flag=QualityFlag.VALID,
        checksum_valid=True,
    )
    # A Person aggregating two stored DOBs is not, by itself, a contradiction.
    holder = _evidence("person-1", dobs=("1980-01-01", "1990-01-01"))
    member = _evidence("sr-1", dobs=("1980-01-01",))
    contradiction = cross_member_contradiction(
        [member], identifier, "onediver", RecordType.IDENTITY, durable_holders=[holder]
    )
    # ...but a member disagreeing with any stored value is.
    assert contradiction is True
    agreeing = _evidence("sr-2", dobs=())
    assert (
        cross_member_contradiction(
            [agreeing], identifier, "onediver", RecordType.IDENTITY, durable_holders=[holder]
        )
        is False
    )
    # A single record asserting two DOBs contradicts itself.
    assert cross_member_contradiction(
        [_evidence("sr-3", dobs=("1980-01-01", "1990-01-01"))],
        identifier,
        "onediver",
        RecordType.IDENTITY,
    )
