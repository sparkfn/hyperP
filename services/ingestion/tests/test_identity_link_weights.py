"""Unit coverage for the C4 link-weight formula.

Direct formula tests plus the shared evidence vectors in
``fixtures/link_weight_vectors.json`` (also read by the API mirror test).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from src.identity_link_weights import (
    CANDIDATE_ONLY_CAP,
    LINK_POLICY_VERSION,
    age_days_between,
    assertion_score,
    combine_weights,
    confidence_weight,
    decay_factor,
    exclusivity_for_count,
    half_life_for,
    noisy_or,
    quality_weight,
    round6,
    tier_weight,
)
from src.identity_links import AssertionEvidence, aggregate_link

_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "link_weight_vectors.json"


class VectorAssertion(TypedDict):
    """One vector assertion row with JSON-validated primitive fields."""

    source_record_pk: str
    trust_tier: str | None
    quality_flag: str | None
    is_verified: bool | None
    extraction_confidence: float | None
    record_type: str | None
    link_status: str | None
    observed_at: str | None


class VectorExpected(TypedDict):
    """Expected link values for one vector."""

    score: float
    exclusivity: float
    rank_score: float


class WeightVector(TypedDict):
    """One shared evidence-to-weight vector."""

    name: str
    identifier_type: str
    participant_count: int | None
    reference_at: str | None
    assertions: list[VectorAssertion]
    expected: VectorExpected


def _require_str(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"link weight vector field {key!r} must be a string")
    return value


def _optional_str(value: object, key: str) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError(f"link weight vector field {key!r} must be a string or null")


def _optional_bool(value: object, key: str) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"link weight vector field {key!r} must be a boolean or null")


def _optional_float(value: object, key: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"link weight vector field {key!r} must be a number or null")
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"link weight vector field {key!r} must be a number or null")


def _optional_int(value: object, key: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"link weight vector field {key!r} must be an integer or null")
    if isinstance(value, int):
        return value
    raise ValueError(f"link weight vector field {key!r} must be an integer or null")


def _require_float(value: object, key: str) -> float:
    result = _optional_float(value, key)
    if result is None:
        raise ValueError(f"link weight vector field {key!r} must be a number")
    return result


def _field(entry: object, key: str) -> object:
    if not isinstance(entry, dict):
        raise ValueError("link weight vector entry must be an object")
    value: object = entry.get(key)
    return value


def _parse_assertion(entry: object) -> VectorAssertion:
    return VectorAssertion(
        source_record_pk=_require_str(_field(entry, "source_record_pk"), "source_record_pk"),
        trust_tier=_optional_str(_field(entry, "trust_tier"), "trust_tier"),
        quality_flag=_optional_str(_field(entry, "quality_flag"), "quality_flag"),
        is_verified=_optional_bool(_field(entry, "is_verified"), "is_verified"),
        extraction_confidence=_optional_float(
            _field(entry, "extraction_confidence"), "extraction_confidence"
        ),
        record_type=_optional_str(_field(entry, "record_type"), "record_type"),
        link_status=_optional_str(_field(entry, "link_status"), "link_status"),
        observed_at=_optional_str(_field(entry, "observed_at"), "observed_at"),
    )


def _parse_expected(entry: object) -> VectorExpected:
    return VectorExpected(
        score=_require_float(_field(entry, "score"), "score"),
        exclusivity=_require_float(_field(entry, "exclusivity"), "exclusivity"),
        rank_score=_require_float(_field(entry, "rank_score"), "rank_score"),
    )


def _parse_vector(entry: object) -> WeightVector:
    raw_assertions = _field(entry, "assertions")
    if not isinstance(raw_assertions, list):
        raise ValueError("link weight vector needs an 'assertions' list")
    return WeightVector(
        name=_require_str(_field(entry, "name"), "name"),
        identifier_type=_require_str(_field(entry, "identifier_type"), "identifier_type"),
        participant_count=_optional_int(_field(entry, "participant_count"), "participant_count"),
        reference_at=_optional_str(_field(entry, "reference_at"), "reference_at"),
        assertions=[_parse_assertion(item) for item in raw_assertions],
        expected=_parse_expected(_field(entry, "expected")),
    )


def _load_vectors() -> list[WeightVector]:
    data: object = json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("link weight vectors must be a JSON object")
    raw_vectors: object = data.get("vectors")
    if not isinstance(raw_vectors, list):
        raise ValueError("link weight vectors need a 'vectors' list")
    return [_parse_vector(entry) for entry in raw_vectors]


def _evidence_from_vector(vector: WeightVector) -> list[AssertionEvidence]:
    rows: list[AssertionEvidence] = []
    for item in vector["assertions"]:
        rows.append(
            AssertionEvidence(
                source_record_pk=item["source_record_pk"],
                identifier_type=vector["identifier_type"],
                identifier_scope="global",
                normalized_value="+6591234567",
                quality_flag=item["quality_flag"],
                is_verified=item["is_verified"],
                trust_tier=item["trust_tier"],
                observed_at=item["observed_at"],
                extraction_confidence=item["extraction_confidence"],
                record_type=item["record_type"],
                link_status=item["link_status"],
                participant_count=vector["participant_count"],
            )
        )
    return rows


def _score(
    *,
    trust_tier: str | None = "tier_3",
    quality_flag: str | None = "valid",
    is_verified: bool | None = False,
    complete: bool = True,
    extraction_confidence: float | None = None,
    record_type: str | None = "identity",
) -> float:
    return assertion_score(
        trust_tier=trust_tier,
        quality_flag=quality_flag,
        is_verified=is_verified,
        complete=complete,
        extraction_confidence=extraction_confidence,
        record_type=record_type,
    )


def test_assertion_score_is_bounded() -> None:
    assert round6(_score()) == 0.6
    assert (
        round6(
            assertion_score(
                trust_tier="tier_1",
                quality_flag="valid",
                is_verified=True,
                complete=True,
                extraction_confidence=1.0,
                record_type="identity",
            )
        )
        == 1.0
    )
    assert round6(_score(extraction_confidence=0.0)) == 0.0
    assert 0.0 <= _score(trust_tier="tier_4", quality_flag="partial_parse") <= 1.0


def test_verified_boost_multiplies_by_1_15() -> None:
    assert round6(_score(is_verified=False)) == 0.6
    assert round6(_score(is_verified=True)) == 0.69
    assert round6(_score(is_verified=None, complete=False)) == 0.35


def test_missing_tier_and_quality_fall_back_to_defaults() -> None:
    assert tier_weight(None, record_type="identity", complete=True) == 0.35
    assert tier_weight("tier_1", record_type="identity", complete=False) == 0.35
    assert tier_weight("unknown", record_type="identity", complete=True) == 0.35
    assert quality_weight(None) == 0.6
    assert quality_weight("partial_parse") == 0.6
    assert quality_weight("valid") == 1.0
    assert confidence_weight(None) == 1.0
    assert round6(_score(trust_tier=None, quality_flag=None, is_verified=None, complete=False)) == (
        0.35 * 0.6
    )


def test_conversation_record_drops_one_tier() -> None:
    assert tier_weight("tier_2", record_type="conversation", complete=True) == 0.60
    assert tier_weight("tier_4", record_type="conversation", complete=True) == 0.35
    assert round6(_score(record_type="conversation")) == 0.35
    assert round6(_score(trust_tier="tier_2", record_type="conversation")) == 0.6


def test_noisy_or_single_monotonic_and_bounded() -> None:
    assert round6(noisy_or([0.6])) == 0.6
    assert noisy_or([]) == 0.0
    two = noisy_or([0.85, 0.85])
    assert round6(two) == 0.9775
    assert two > 0.85
    assert noisy_or([0.6, 0.6, 0.6]) < 1.0
    assert round6(combine_weights([0.6], [])) == 0.6


def test_candidate_only_cap_and_mixed_combination() -> None:
    assert round6(combine_weights([], [1.0, 1.0])) == CANDIDATE_ONLY_CAP
    assert round6(combine_weights([], [1.0])) == 0.24
    assert round6(combine_weights([0.6], [1.0])) == 0.696
    # Candidate-only evidence never reduces confirmed evidence.
    assert combine_weights([0.6], [0.5]) > 0.6
    assert round6(combine_weights([1.0], [1.0])) == 1.0


def test_decay_factor_half_life_and_none() -> None:
    assert decay_factor(365.0, 365) == 0.5
    assert decay_factor(0.0, 365) == 1.0
    assert decay_factor(None, 365) == 1.0
    assert decay_factor(365.0, None) == 1.0
    assert decay_factor(730.0, 730) == 0.5
    assert half_life_for("phone") == 365
    assert half_life_for("email") == 730
    assert half_life_for("device_id") == 180
    assert half_life_for("social:whatsapp") == 365
    assert half_life_for("nric") is None


def test_exclusivity_is_inverse_count() -> None:
    assert exclusivity_for_count(1) == 1.0
    assert exclusivity_for_count(2) == 0.5
    assert exclusivity_for_count(4) == 0.25
    assert exclusivity_for_count(None) == 1.0
    assert exclusivity_for_count(0) == 1.0


def test_age_days_uses_evidence_time_not_wall_clock() -> None:
    assert age_days_between("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z") == 365.0
    assert age_days_between("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z") == 0.0
    assert age_days_between(None, "2026-01-01T00:00:00Z") is None
    assert age_days_between("2026-01-01T00:00:00Z", None) is None
    assert age_days_between("not-a-time", "2026-01-01T00:00:00Z") is None


def test_shuffled_inputs_are_deterministic() -> None:
    vectors = _load_vectors()
    assert vectors, "shared vectors must not be empty"
    for vector in vectors:
        rows = _evidence_from_vector(vector)
        first = aggregate_link(rows, reference_at=vector["reference_at"])
        reversed_rows = aggregate_link(list(reversed(rows)), reference_at=vector["reference_at"])
        assert (reversed_rows.score, reversed_rows.exclusivity, reversed_rows.rank_score) == (
            first.score,
            first.exclusivity,
            first.rank_score,
        ), vector["name"]


def test_shared_vectors_match_formula() -> None:
    for vector in _load_vectors():
        aggregate = aggregate_link(
            _evidence_from_vector(vector), reference_at=vector["reference_at"]
        )
        expected = vector["expected"]
        assert aggregate.score == expected["score"], vector["name"]
        assert aggregate.exclusivity == expected["exclusivity"], vector["name"]
        assert aggregate.rank_score == expected["rank_score"], vector["name"]
        assert aggregate.policy_version == LINK_POLICY_VERSION, vector["name"]
