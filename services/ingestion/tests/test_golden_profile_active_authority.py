"""Focused compatibility coverage for active-authority golden-profile recomputation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from neo4j.time import DateTime
from src.golden_profile import recompute_golden_profile_from_active_authority
from src.graph.queries.persons import (
    FETCH_ACTIVE_PERSON_AUTHORITY_WITH_OVERRIDES,
    FETCH_ADDRESS_IDS_BY_NORMALIZED_FULL,
    FETCH_PERSON_ADDRESSES,
    FETCH_PERSON_FACTS,
    FETCH_PERSON_IDENTIFIERS,
)


@dataclass
class _Row:
    values: dict[str, object]

    def __getitem__(self, key: str) -> object:
        return self.values[key]


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def single(self) -> _Row | None:
        return self._row

    def consume(self) -> None:
        return None


class _Transaction:
    def __init__(self, row: _Row) -> None:
        self.row = row
        self.writes: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        if query == FETCH_ACTIVE_PERSON_AUTHORITY_WITH_OVERRIDES:
            return _Result(self.row)
        if query == FETCH_ADDRESS_IDS_BY_NORMALIZED_FULL:
            return _Result(_Row({"address_ids": ["address-custom"]}))
        self.writes.append((query, params))
        return _Result(None)


def _row(
    *,
    overrides: object = None,
    facts: list[dict[str, object]] | None = None,
    identifiers: list[dict[str, object]] | None = None,
    addresses: list[dict[str, object]] | None = None,
    current_name: str | None = None,
) -> _Row:
    return _Row(
        {
            "person": {
                "person_id": "person-a",
                "survivorship_overrides": overrides,
                "preferred_full_name": current_name,
                "preferred_dob": None,
                "preferred_phone": None,
                "preferred_email": None,
                "preferred_address_id": None,
                "preferred_nric": None,
                "preferred_race_ethnicity": None,
                "profile_completeness_score": 0.2 if current_name is not None else 0.0,
                "golden_profile_version": "v0.1.0",
            },
            "facts": facts or [],
            "identifiers": identifiers or [],
            "addresses": addresses or [],
        }
    )


def _fact(value: str, *, source: str = "source-a", quality: str = "valid") -> dict[str, object]:
    return {
        "attribute_name": "full_name",
        "attribute_value": value,
        "source_trust_tier": "tier_1",
        "observed_at": "2026-09-01T00:00:00+00:00",
        "quality_flag": quality,
        "source_record_pk": source,
    }


def test_active_authority_no_override_parity_and_no_write_when_unchanged() -> None:
    tx = _Transaction(_row(facts=[_fact("Alice")], current_name="Alice"))
    result = recompute_golden_profile_from_active_authority(
        tx, "person-a", invalidate_analysis=True
    )
    assert result is not None
    assert result.profile["preferred_full_name"] == "Alice"
    assert result.changed is False
    assert tx.writes == []


def test_invalid_facts_are_excluded_and_changed_profile_writes_once() -> None:
    tx = _Transaction(_row(facts=[_fact("Ignored", quality="invalid_format"), _fact("Alice")]))
    result = recompute_golden_profile_from_active_authority(
        tx, "person-a", invalidate_analysis=False
    )
    assert result is not None and result.profile["preferred_full_name"] == "Alice"
    assert len(tx.writes) == 1


def test_custom_and_active_source_backed_fact_identifier_and_address_overrides() -> None:
    overrides = (
        '{"preferred_full_name":{"custom_value":"Custom"},'
        '"preferred_phone":{"source_record_pk":"phone-source"},'
        '"preferred_address":{"source_record_pk":"address-source"}}'
    )
    tx = _Transaction(
        _row(
            overrides=overrides,
            facts=[_fact("Authority")],
            identifiers=[
                {
                    "identifier_type": "phone",
                    "normalized_value": "+6500000000",
                    "is_verified": True,
                    "last_confirmed_at": "2026-09-01T00:00:00+00:00",
                    "source_record_pk": "phone-source",
                }
            ],
            addresses=[
                {
                    "address_id": "address-a",
                    "normalized_full": "1 Main Street",
                    "is_verified": True,
                    "last_confirmed_at": "2026-09-01T00:00:00+00:00",
                    "source_record_pk": "address-source",
                }
            ],
        )
    )
    result = recompute_golden_profile_from_active_authority(
        tx, "person-a", invalidate_analysis=False
    )
    assert result is not None
    assert result.profile["preferred_full_name"] == "Custom"
    assert result.profile["preferred_phone"] == "+6500000000"
    assert result.profile["preferred_address_id"] == "address-a"
    assert result.conflict_fields == ()


def test_api_shaped_custom_preferred_address_resolves_without_active_link() -> None:
    raw = '{"preferred_address":{"custom_value":"1 Main Street"}}'
    tx = _Transaction(_row(overrides=raw))
    result = recompute_golden_profile_from_active_authority(
        tx, "person-a", invalidate_analysis=False
    )
    assert result is not None
    assert result.profile["preferred_address_id"] == "address-custom"
    assert result.conflict_fields == ()
    assert tx.row.values["person"]["survivorship_overrides"] == raw


def test_retired_source_malformed_and_unknown_overrides_conflict_without_rewrite() -> None:
    raw = '{"preferred_full_name":{"source_record_pk":"retired"},"unknown":{},"preferred_phone":7}'
    tx = _Transaction(_row(overrides=raw, facts=[_fact("Authority")]))
    result = recompute_golden_profile_from_active_authority(
        tx, "person-a", invalidate_analysis=False
    )
    assert result is not None
    assert result.profile["preferred_full_name"] == "Authority"
    assert set(result.conflict_fields) == {"preferred_full_name", "preferred_phone", "unknown"}
    assert tx.row.values["person"].get("survivorship_overrides") == raw


def test_changed_profile_optionally_invalidates_once() -> None:
    tx = _Transaction(_row(facts=[_fact("Alice")]))
    result = recompute_golden_profile_from_active_authority(
        tx, "person-a", invalidate_analysis=True
    )
    assert result is not None and result.changed
    assert len(tx.writes) == 2


def test_legacy_entrypoint_stays_available_after_active_authority_split() -> None:
    from src.golden_profile import compute_golden_profile

    assert compute_golden_profile.__name__ == "compute_golden_profile"


class _LegacyResult:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[_Row]:
        return iter(self._rows)


class _LegacyTransaction:
    def __init__(self) -> None:
        self.writes: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _LegacyResult:
        if query == FETCH_PERSON_FACTS:
            return _LegacyResult(
                [
                    _Row(
                        {
                            "attribute_name": "full_name",
                            "attribute_value": "Alice",
                            "quality_flag": "valid",
                            "source_trust_tier": "tier_1",
                            "observed_at": "2026-09-01T00:00:00+00:00",
                        }
                    )
                ]
            )
        if query == FETCH_PERSON_IDENTIFIERS:
            return _LegacyResult([])
        if query == FETCH_PERSON_ADDRESSES:
            return _LegacyResult([])
        self.writes.append((query, params))
        return _LegacyResult([])


def test_legacy_compute_profile_survivorship_output_remains_compatible() -> None:
    from src.golden_profile import compute_golden_profile

    tx = _LegacyTransaction()
    profile = compute_golden_profile(tx, "person-a")
    assert profile["preferred_full_name"] == "Alice"
    assert profile["profile_completeness_score"] == 0.2
    assert len(tx.writes) == 1


def test_legacy_compute_profile_accepts_real_neo4j_datetime_evidence() -> None:
    from src.golden_profile import compute_golden_profile

    class _Neo4jTemporalTransaction(_LegacyTransaction):
        def run(self, query: str, **params: object) -> _LegacyResult:
            if query == FETCH_PERSON_FACTS:
                return _LegacyResult(
                    [
                        _Row(
                            {
                                "attribute_name": "full_name",
                                "attribute_value": "Ada",
                                "quality_flag": "valid",
                                "source_trust_tier": "tier_1",
                                "observed_at": DateTime(2020, 1, 1, 0, 0, 0),
                            }
                        )
                    ]
                )
            return super().run(query, **params)

    profile = compute_golden_profile(_Neo4jTemporalTransaction(), "person-a")
    assert profile["preferred_full_name"] == "Ada"
