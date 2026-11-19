"""Tests for deterministic matching using values list matching legacy candidates and digests."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.matching.deterministic import (
    _PERSON_HAS_CONFLICTING_GOVT_ID,
    _PERSON_HAS_VALID_GOVT_ID,
    _check_government_id,
)
from src.models import (
    MatchDecision,
    NormalizedIdentifier,
    QualityFlag,
)
from src.normalizers.nric import nric_digest

TEST_KEY = "test-nric-hmac-key-v1"


def _make_candidate_tx(candidate_nrics: list[str]) -> MagicMock:
    """Mock transaction where _PERSON_HAS_VALID_GOVT_ID checks intersection with candidate_nrics,
    and _PERSON_HAS_CONFLICTING_GOVT_ID checks if candidate_nrics has any value not in query values.
    """
    tx = MagicMock()

    def mock_run(query: str, *, person_id: str, values: list[str]) -> MagicMock:
        val_set = set(values)
        cand_set = set(candidate_nrics)
        if query == _PERSON_HAS_VALID_GOVT_ID:
            # Matches if there is intersection
            if val_set & cand_set:
                return MagicMock(single=lambda: {"person_id": person_id})
            return MagicMock(single=lambda: None)
        if query == _PERSON_HAS_CONFLICTING_GOVT_ID:
            # Matches if candidate has any NRIC not in values
            conflicting = cand_set - val_set
            if conflicting:
                return MagicMock(single=lambda: {"conflicting_value": next(iter(conflicting))})
            return MagicMock(single=lambda: None)
        return MagicMock(single=lambda: None)

    tx.run = mock_run
    return tx


def test_deterministic_matching_cases() -> None:
    digest1 = nric_digest("S1234567D", TEST_KEY)
    plaintext1 = "S1234567D"
    other_digest = nric_digest("T1234567J", TEST_KEY)
    other_plaintext = "T1234567J"

    incoming = [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value=digest1,
            quality_flag=QualityFlag.VALID,
            nric_plaintext=plaintext1,
        )
    ]

    # 1. Candidate legacy plaintext equal (incoming has digest + plaintext carrier) -> MERGE
    tx1 = _make_candidate_tx([plaintext1])
    res1 = _check_government_id(tx1, "p-1", incoming)
    assert res1 is not None
    assert res1.decision == MatchDecision.MERGE
    assert res1.confidence == 1.0

    # 2. Candidate legacy plaintext different -> hard NO_MATCH
    tx2 = _make_candidate_tx([other_plaintext])
    res2 = _check_government_id(tx2, "p-1", incoming)
    assert res2 is not None
    assert res2.decision == MatchDecision.NO_MATCH

    # 3. Candidate digest equal -> MERGE
    tx3 = _make_candidate_tx([digest1])
    res3 = _check_government_id(tx3, "p-1", incoming)
    assert res3 is not None
    assert res3.decision == MatchDecision.MERGE

    # 4. Candidate digest different -> hard NO_MATCH
    tx4 = _make_candidate_tx([other_digest])
    res4 = _check_government_id(tx4, "p-1", incoming)
    assert res4 is not None
    assert res4.decision == MatchDecision.NO_MATCH

    # 5. C1-shaped candidate (64-hex, no metadata) equal digest -> MERGE
    tx5 = _make_candidate_tx([digest1])
    # incoming is a C1-shaped row without plaintext carrier
    c1_incoming = [
        NormalizedIdentifier(
            identifier_type="nric",
            normalized_value=digest1,
            quality_flag=QualityFlag.VALID,
            nric_plaintext=None,
        )
    ]
    res5 = _check_government_id(tx5, "p-1", c1_incoming)
    assert res5 is not None
    assert res5.decision == MatchDecision.MERGE

    # 6. Candidate has no NRIC -> None
    tx6 = _make_candidate_tx([])
    res6 = _check_government_id(tx6, "p-1", incoming)
    assert res6 is None
