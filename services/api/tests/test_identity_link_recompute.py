"""Pure aggregation checks for the API HAS_IDENTIFIER maintenance."""

from __future__ import annotations

from src.graph.identity_link_recompute import build_link_rows


def _evidence(
    source_record_pk: str,
    trust_tier: str | None,
    *,
    quality_flag: str | None = "valid",
    is_verified: bool | None = True,
) -> dict[str, object]:
    return {
        "source_record_pk": source_record_pk,
        "identifier_id": "ident-phone",
        "identifier_type": "phone",
        "identifier_scope": "global",
        "normalized_value": "+6591234567",
        "quality_flag": quality_flag,
        "is_verified": is_verified,
        "trust_tier": trust_tier,
        "observed_at": "2026-01-01T00:00:00Z",
    }


def test_aggregate_prefers_the_highest_trust_tier() -> None:
    rows, link_ids = build_link_rows(
        "person-1",
        [_evidence("sr-1", "tier_1"), _evidence("sr-2", "tier_4")],
    )

    assert rows[0]["max_tier"] == "tier_1"
    assert rows[0]["verified_count"] == 2
    assert link_ids == [rows[0]["link_id"]]


def test_aggregate_incomplete_metadata_falls_back_to_the_lowest_tier() -> None:
    incomplete = _evidence("sr-1", "tier_1", quality_flag=None, is_verified=None)
    rows, _link_ids = build_link_rows(
        "person-1",
        [incomplete, _evidence("sr-2", "tier_4")],
    )

    assert rows[0]["max_tier"] == "tier_4"
    assert rows[0]["verified_count"] == 1


def test_aggregate_with_only_incomplete_metadata_uses_the_lowest_tier() -> None:
    rows, _link_ids = build_link_rows(
        "person-1",
        [_evidence("sr-1", "tier_1", quality_flag=None, is_verified=None)],
    )

    assert rows[0]["max_tier"] == "tier_4"
    assert rows[0]["verified_count"] == 0
