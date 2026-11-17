"""Unit coverage for the top-3 golden profile stage (§2.2)."""

from __future__ import annotations

import json
from typing import cast

from neo4j import ManagedTransaction
from src.golden_profile_authority_models import OverrideEntry
from src.golden_top3 import (
    GoldenTop3Outcome,
    TopLink,
    build_conflicts,
    build_golden_payload,
    materialize_golden_top3,
    rank_links,
    resolve_pins,
)
from src.graph import queries
from src.nric_anchor import LinkDecision

_NRIC_A = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_NRIC_B = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"


class _FakeTx:
    """Records query/param pairs and answers canned top-link rows."""

    def __init__(self, rows_by_query: dict[str, list[dict[str, object]]]) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._rows = rows_by_query

    def run(self, query: str, **params: object) -> list[dict[str, object]]:
        self.calls.append((query, dict(params)))
        return [dict(row) for row in self._rows.get(query, [])]


def _link_row(
    *,
    link_id: str = "link-1",
    identifier_type: str = "phone",
    normalized_value: str = "+6591234567",
    rank_score: float = 0.9,
    verified_count: int = 1,
    last_confirmed_at: str | None = "2026-02-01T00:00:00Z",
    source_count: int = 1,
    active_evidence_count: int = 1,
    evidence_pks: list[str] | None = None,
    nric_masked: str | None = None,
    overrides: str | None = None,
    stored_json: str | None = None,
    stored_conflicts: str | None = None,
) -> dict[str, object]:
    return {
        "survivorship_overrides": overrides,
        "stored_golden_json": stored_json,
        "stored_golden_conflicts": stored_conflicts,
        "link_id": link_id,
        "identifier_type": identifier_type,
        "identifier_scope": "global",
        "normalized_value": normalized_value,
        "nric_masked": nric_masked,
        "score": rank_score,
        "exclusivity": 1.0,
        "rank_score": rank_score,
        "verified_count": verified_count,
        "last_confirmed_at": last_confirmed_at,
        "source_count": source_count,
        "active_evidence_count": active_evidence_count,
        "evidence_pks": evidence_pks if evidence_pks is not None else ["sr-1"],
    }


def _phone(
    value: str,
    score: float,
    *,
    active_evidence_count: int = 1,
    verified_count: int = 1,
    last_confirmed_at: str | None = "2026-02-01T00:00:00Z",
) -> dict[str, object]:
    return _link_row(
        link_id=f"link-{value}",
        normalized_value=value,
        rank_score=score,
        active_evidence_count=active_evidence_count,
        verified_count=verified_count,
        last_confirmed_at=last_confirmed_at,
    )


def _top_link(
    link_id: str,
    identifier_type: str,
    normalized_value: str,
    rank_score: float,
    *,
    verified: bool = True,
    last_confirmed_at: str | None = "2026-02-01T00:00:00Z",
    candidate_only: bool = False,
) -> TopLink:
    return TopLink(
        link_id=link_id,
        identifier_type=identifier_type,
        identifier_scope="global",
        normalized_value=normalized_value,
        score=rank_score,
        exclusivity=1.0,
        rank_score=rank_score,
        verified=verified,
        last_confirmed_at=last_confirmed_at,
        source_count=1,
        candidate_only=candidate_only,
        nric_masked=None,
        evidence_pks=("sr-1",),
    )


def _materialize(rows: list[dict[str, object]]) -> tuple[_FakeTx, GoldenTop3Outcome]:
    fake = _FakeTx({queries.FETCH_PERSON_TOP_LINKS: rows, queries.FETCH_PERSON_LINK_KEYS: []})
    outcome = materialize_golden_top3(cast(ManagedTransaction, fake), "person-1")
    return fake, outcome


def _write_params(fake: _FakeTx, query: str) -> list[dict[str, object]]:
    return [params for issued, params in fake.calls if issued == query]


def _set_rows(fake: _FakeTx) -> list[dict[str, object]]:
    calls = _write_params(fake, queries.SET_LINK_GOLDEN_RANKS)
    assert len(calls) == 1
    rows = calls[0].get("rows")
    assert isinstance(rows, list)
    parsed: list[dict[str, object]] = []
    for row in rows:
        assert isinstance(row, dict)
        parsed.append({str(key): value for key, value in row.items()})
    return parsed


def test_ranks_top_three_per_type_and_leaves_rest_unranked() -> None:
    rows = [
        _phone("+6590000001", 0.9),
        _phone("+6590000002", 0.8),
        _phone("+6590000003", 0.7),
        _phone("+6590000004", 0.6),
        _link_row(
            link_id="link-device",
            identifier_type="device_id",
            normalized_value="device-1",
            rank_score=0.99,
        ),
        _phone("+6590000005", 0.95, active_evidence_count=0),
    ]
    fake, outcome = _materialize(rows)

    written = {str(row["link_id"]): row["golden_rank"] for row in _set_rows(fake)}
    assert written == {
        "link-+6590000001": 1,
        "link-+6590000002": 2,
        "link-+6590000003": 3,
        "link-+6590000004": None,
        "link-device": None,
        "link-+6590000005": None,
    }
    assert outcome.picks["phone"] == "+6590000001"
    assert outcome.picks["email"] is None
    assert outcome.golden_json is not None
    assert json.loads(outcome.golden_json)["phone"]["value"] == "+6590000001"


def test_rank1_equals_preferred_picks_with_masked_nric_and_conflict() -> None:
    rows = [
        _link_row(
            link_id="link-nric-a",
            identifier_type="nric",
            normalized_value=_NRIC_A,
            rank_score=0.9,
            nric_masked="S****123A",
        ),
        _link_row(
            link_id="link-nric-b",
            identifier_type="nric",
            normalized_value=_NRIC_B,
            rank_score=0.8,
            nric_masked="S****456B",
        ),
    ]
    _fake, outcome = _materialize(rows)

    assert outcome.picks["nric"] == "S****123A"
    assert outcome.golden_json is not None
    assert json.loads(outcome.golden_json)["nric"] == {
        "value": "S****123A",
        "rank": 1,
        "score": 0.9,
        "verified": True,
        "last_confirmed_at": "2026-02-01T00:00:00Z",
        "source_count": 1,
    }
    assert json.loads(outcome.golden_conflicts) == [
        {"type": "nric", "reason": "multiple_valid_values", "values": ["S****123A", "S****456B"]}
    ]


def test_pin_by_source_record_pk_outranks_score() -> None:
    overrides = json.dumps({"preferred_phone": {"source_record_pk": "sr-pin"}})
    rows = [
        _link_row(
            link_id="link-low",
            normalized_value="+6590000001",
            rank_score=0.5,
            evidence_pks=["sr-pin"],
            overrides=overrides,
        ),
        _link_row(
            link_id="link-high",
            normalized_value="+6590000002",
            rank_score=0.9,
            evidence_pks=["sr-other"],
            overrides=overrides,
        ),
    ]
    _fake, outcome = _materialize(rows)

    assert outcome.picks["phone"] == "+6590000001"
    assert outcome.golden_json is not None
    assert json.loads(outcome.golden_json)["phone"]["value"] == "+6590000001"


def test_pin_by_custom_value_matches_normalized_email() -> None:
    overrides = json.dumps({"preferred_email": {"custom_value": "User@Example.com"}})
    rows = [
        _link_row(
            link_id="link-a",
            identifier_type="email",
            normalized_value="user@example.com",
            rank_score=0.5,
            overrides=overrides,
        ),
        _link_row(
            link_id="link-b",
            identifier_type="email",
            normalized_value="other@example.com",
            rank_score=0.9,
            overrides=overrides,
        ),
    ]
    _fake, outcome = _materialize(rows)

    assert outcome.picks["email"] == "user@example.com"


def test_unresolved_pin_yields_conflict_and_keeps_score_order() -> None:
    overrides = json.dumps({"preferred_phone": {"source_record_pk": "sr-missing"}})
    rows = [
        _link_row(normalized_value="+6590000001", rank_score=0.5, overrides=overrides),
        _link_row(
            link_id="link-2",
            normalized_value="+6590000002",
            rank_score=0.9,
            overrides=overrides,
        ),
    ]
    _fake, outcome = _materialize(rows)

    assert outcome.picks["phone"] == "+6590000002"
    assert json.loads(outcome.golden_conflicts) == ["pin_unresolved:preferred_phone"]


def test_candidate_only_links_are_unranked_and_write_no_document() -> None:
    rows = [_phone("+6590000001", 0.2, active_evidence_count=0)]
    fake, outcome = _materialize(rows)

    assert _set_rows(fake) == [{"link_id": "link-+6590000001", "golden_rank": None}]
    assert _write_params(fake, queries.UPDATE_GOLDEN_TOP3) == []
    assert outcome.picks == {"phone": None, "email": None, "nric": None}
    assert outcome.golden_json is None


def test_no_links_and_no_stored_state_writes_nothing() -> None:
    fake, outcome = _materialize([])

    assert [query for query, _ in fake.calls] == [
        queries.LOCK_PERSON_FOR_GOLDEN,
        queries.FETCH_PERSON_LINK_KEYS,
        queries.FETCH_PERSON_TOP_LINKS,
    ]
    assert outcome.golden_json is None


def test_device_only_links_with_stored_state_clear_the_document() -> None:
    rows = [
        _link_row(
            link_id="link-device",
            identifier_type="device_id",
            normalized_value="device-1",
            rank_score=0.9,
            stored_json='{"phone": {}}',
            stored_conflicts="[]",
        )
    ]
    fake, outcome = _materialize(rows)

    update_calls = _write_params(fake, queries.UPDATE_GOLDEN_TOP3)
    assert len(update_calls) == 1
    assert update_calls[0]["golden_json"] is None
    assert update_calls[0]["golden_conflicts"] == "[]"
    assert outcome.golden_json is None


def test_ordering_tiebreaks_verified_time_and_value() -> None:
    rows = [
        _phone("+6590000001", 0.5, verified_count=1, last_confirmed_at="2026-01-01T00:00:00Z"),
        _phone("+6590000002", 0.5, verified_count=1, last_confirmed_at="2026-02-01T00:00:00Z"),
        _phone("+6590000003", 0.5, verified_count=0, last_confirmed_at="2026-03-01T00:00:00Z"),
    ]
    _fake, outcome = _materialize(rows)

    assert outcome.picks["phone"] == "+6590000002"
    assert outcome.golden_json is not None
    payload = json.loads(outcome.golden_json)
    assert payload["phone"]["last_confirmed_at"] == "2026-02-01T00:00:00Z"


def test_normalized_value_breaks_remaining_ties() -> None:
    links = [
        _top_link("link-b", "phone", "+6590000005", 0.5),
        _top_link("link-a", "phone", "+6590000004", 0.5),
    ]
    ranked = rank_links(links, set())

    assert [(item.link.link_id, item.rank) for item in ranked] == [("link-b", 2), ("link-a", 1)]


def test_malformed_link_row_raises() -> None:
    rows = [_link_row()]
    rows[0]["rank_score"] = "high"
    fake = _FakeTx({queries.FETCH_PERSON_TOP_LINKS: rows, queries.FETCH_PERSON_LINK_KEYS: []})
    try:
        materialize_golden_top3(cast(ManagedTransaction, fake), "person-1")
    except ValueError:
        return
    raise AssertionError("malformed link row must raise ValueError")


def test_resolve_pins_builds_link_decisions() -> None:
    links = [
        _top_link("link-1", "phone", "+6590000001", 0.5),
    ]
    parsed = resolve_pins(
        links,
        (OverrideEntry("preferred_phone", None, "sr-1", False, False),),
        "person-1",
    )
    assert set(parsed.pinned) == {"link-1"}
    assert isinstance(parsed.pinned["link-1"], LinkDecision)
    assert parsed.pinned["link-1"].kind == "pin"
    assert parsed.unresolved == ()


def test_build_payload_and_conflicts_helpers() -> None:
    ranked = rank_links([], set())
    assert build_golden_payload(ranked) == {}
    assert build_conflicts(ranked, []) == []


def test_nric_without_mask_renders_null_value() -> None:
    links = [_top_link("link-1", "nric", _NRIC_A, 0.9)]
    ranked = rank_links(links, set())
    payload = build_golden_payload(ranked)

    assert payload["nric"]["value"] is None


def test_determinism_across_runs() -> None:
    rows = [
        _phone("+6590000002", 0.8),
        _phone("+6590000001", 0.9),
        _link_row(
            link_id="link-nric",
            identifier_type="nric",
            normalized_value=_NRIC_A,
            rank_score=0.9,
            nric_masked="S****123A",
        ),
    ]
    fake_a, outcome_a = _materialize([dict(row) for row in rows])
    fake_b, outcome_b = _materialize([dict(row) for row in rows])

    assert outcome_a == outcome_b
    assert fake_a.calls == fake_b.calls
