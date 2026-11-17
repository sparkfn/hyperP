"""Unit coverage for Person.golden_top parsing (plan §2.7)."""

from __future__ import annotations

import json

from src.graph.mappers import map_person
from src.graph.mappers_golden import parse_golden_top
from src.graph.mappers_profiles import map_unlinked_profile
from src.routes.public_pages import _strip_public_person
from src.types import GoldenTopConflict, Person


def _person_record(golden_json: str | None, golden_conflicts: str | None) -> dict[str, object]:
    return {
        "person": {
            "person_id": "person-1",
            "status": "active",
            "preferred_phone": "+6591234567",
            "golden_json": golden_json,
            "golden_conflicts": golden_conflicts,
        }
    }


def _golden_json() -> str:
    return json.dumps(
        {
            "email": [
                {
                    "value": "user@example.com",
                    "rank": 1,
                    "score": 0.9,
                    "verified": True,
                    "last_confirmed_at": "2026-02-01T00:00:00Z",
                    "source_count": 2,
                }
            ],
            "nric": [
                {
                    "value": "S****123A",
                    "rank": 1,
                    "score": 1.0,
                    "verified": True,
                    "last_confirmed_at": "2026-01-01T00:00:00Z",
                    "source_count": 1,
                }
            ],
            "phone": [
                {
                    "value": "+6591234567",
                    "rank": 1,
                    "score": 0.95,
                    "verified": False,
                    "last_confirmed_at": None,
                    "source_count": 3,
                },
                {
                    "value": "+6597654321",
                    "rank": 2,
                    "score": 0.6,
                    "verified": True,
                    "last_confirmed_at": "2026-03-01T00:00:00Z",
                    "source_count": 1,
                },
            ],
        },
        sort_keys=True,
    )


def test_parse_golden_top_reads_rank_one_values_with_masked_nric() -> None:
    block = parse_golden_top(_golden_json(), "[]")

    assert block is not None
    assert block.phone[0].value == "+6591234567"
    assert block.phone[0].rank == 1
    assert block.phone[0].score == 0.95
    assert block.phone[0].verified is False
    assert block.phone[0].source_count == 3
    assert block.phone[1].value == "+6597654321"
    assert block.phone[1].rank == 2
    assert block.email[0].value == "user@example.com"
    assert block.nric[0].value == "S****123A"
    assert block.conflicts == []


def test_parse_golden_top_includes_conflicts_and_pin_entries() -> None:
    conflicts = json.dumps(
        [
            {"type": "nric", "reason": "multiple_valid_values", "values": ["S****123A"]},
            "pin_unresolved:preferred_phone",
        ]
    )
    block = parse_golden_top(_golden_json(), conflicts)

    assert block is not None
    first = block.conflicts[0]
    assert isinstance(first, GoldenTopConflict)
    assert first.model_dump() == {
        "type": "nric",
        "reason": "multiple_valid_values",
        "values": ["S****123A"],
    }
    assert block.conflicts[1] == "pin_unresolved:preferred_phone"


def test_parse_golden_top_returns_none_for_absent_or_malformed_json() -> None:
    assert parse_golden_top(None, None) is None
    assert parse_golden_top("not-json", "[]") is None
    assert parse_golden_top("[1, 2]", "[]") is None
    assert parse_golden_top(_golden_json(), "not-json") is None
    assert parse_golden_top("{}", "[]") is None


def test_parse_golden_top_drops_malformed_entries() -> None:
    payload = json.dumps({"phone": {"value": 42}, "email": {"value": "user@example.com"}})
    block = parse_golden_top(payload, "[]")

    assert block is not None
    assert block.phone == []
    assert block.email[0].value == "user@example.com"


def test_map_person_carries_golden_top() -> None:
    person = map_person(_person_record(_golden_json(), "[]"))

    assert isinstance(person, Person)
    assert person.golden_top is not None
    assert person.golden_top.phone[0].value == "+6591234567"
    assert person.preferred_phone == "+6591234567"


def test_map_person_without_golden_state_has_no_golden_top() -> None:
    person = map_person({})

    assert person.golden_top is None


def test_map_unlinked_profile_carries_value_score() -> None:
    record: dict[str, object] = {
        "profile": {
            "profile_id": "profile-1",
            "status": "open",
            "evidence_version": 1,
        },
        "record_count": 4,
        "identifier_count": 1,
        "value_score": 2,
    }
    profile = map_unlinked_profile(record)

    assert profile.profile_id == "profile-1"
    assert profile.value_score == 2
    assert profile.record_count == 4


def test_strip_public_person_nulls_golden_top() -> None:
    person = map_person(_person_record(_golden_json(), "[]"))
    assert person.golden_top is not None
    assert len(person.golden_top.phone) == 2

    assert _strip_public_person(person).golden_top is None
