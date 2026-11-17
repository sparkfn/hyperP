"""Unit coverage for the v2 weighted identifier scorer (plan §2.3)."""

from __future__ import annotations

import math
from typing import cast

import pytest
from neo4j import ManagedTransaction
from src.graph import queries
from src.matching.heuristic import evaluate_heuristic
from src.matching.snapshot import CandidateSnapshot
from src.matching.weighted_scoring import idf, weighted_signal
from src.models import (
    JsonValue,
    NormalizedAttribute,
    NormalizedIdentifier,
    QualityFlag,
    RecordType,
)


class _StrictTx:
    """Fails any Cypher query; weighted scoring with a snapshot issues none."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def run(self, query: str, **params: object) -> list[dict[str, object]]:
        self.queries.append(query)
        raise AssertionError(f"unexpected query: {query}")


class _FanoutRow:
    """Single-row result shape mirroring the driver's ``.single()`` contract."""

    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def single(self) -> dict[str, object]:
        return self._row


class _FanoutTx(_StrictTx):
    """Answers the legacy fanout recount with zero fanout."""

    def run(self, query: str, **params: object) -> _FanoutRow:
        self.queries.append(query)
        if query == queries.CHECK_IDENTIFIER_FANOUT:
            return _FanoutRow({"fanout": 0})
        raise AssertionError(f"unexpected query: {query}")


def _phone_ident(value: str, *, verified: bool = True) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type="phone",
        normalized_value=value,
        is_verified=verified,
        quality_flag=QualityFlag.VALID,
    )


def _email_ident(value: str, *, verified: bool = True) -> NormalizedIdentifier:
    return NormalizedIdentifier(
        identifier_type="email",
        normalized_value=value,
        is_verified=verified,
        quality_flag=QualityFlag.VALID,
    )


def _link_row(
    identifier_type: str,
    value: str,
    *,
    verified: bool = True,
    link_weight: float | None = 0.5,
    participant_count: int | None = 1,
    system_sourced: bool = True,
) -> dict[str, object]:
    row: dict[str, object] = {
        "identifier_type": identifier_type,
        "normalized_value": value,
        "is_verified": verified,
        "last_confirmed_at": "2026-01-01T00:00:00Z",
        "is_system_sourced": system_sourced,
    }
    if link_weight is not None:
        row["link_weight"] = link_weight
    if participant_count is not None:
        row["participant_count"] = participant_count
    return row


def _snapshot(
    idents: list[dict[str, object]],
    facts: list[dict[str, object]] | None = None,
    *,
    anchored: bool = False,
) -> CandidateSnapshot:
    return CandidateSnapshot(
        idents=idents, facts=facts if facts is not None else [], addrs=[], anchored=anchored
    )


def _score(
    tx: _StrictTx,
    identifiers: list[NormalizedIdentifier],
    snapshot: CandidateSnapshot,
    attributes: list[NormalizedAttribute] | None = None,
    record_type: RecordType = RecordType.IDENTITY,
    *,
    weighted: bool = True,
) -> tuple[float, _StrictTx, dict[str, JsonValue]]:
    result = evaluate_heuristic(
        cast(ManagedTransaction, tx),
        "person-1",
        identifiers,
        None,
        attributes if attributes is not None else [],
        record_type,
        snapshot=snapshot,
        weighted=weighted,
    )
    return result.confidence, tx, result.feature_snapshot


def _five_phone_links() -> tuple[list[NormalizedIdentifier], CandidateSnapshot]:
    identifiers = [_phone_ident(f"+659000000{i}") for i in range(1, 6)]
    snapshot = _snapshot(
        [_link_row("phone", f"+659000000{i}", link_weight=0.5) for i in range(1, 6)]
    )
    return identifiers, snapshot


def test_fifth_link_beyond_rank_three_still_contributes() -> None:
    identifiers, snapshot = _five_phone_links()
    tx = _StrictTx()
    confidence, tx, _features = _score(tx, identifiers, snapshot)

    assert tx.queries == []
    assert round(confidence, 6) == 0.85
    four_confidence, _, _ = _score(_StrictTx(), identifiers[:4], _snapshot(snapshot.idents[:4]))
    assert round(four_confidence, 6) == 0.7
    assert four_confidence < confidence


def test_no_fanout_query_in_weighted_mode() -> None:
    identifiers, snapshot = _five_phone_links()
    tx = _StrictTx()
    _score(tx, identifiers, snapshot)

    assert queries.CHECK_IDENTIFIER_FANOUT not in tx.queries
    assert tx.queries == []


def test_provisional_link_weight_defaults_to_one() -> None:
    snapshot = _snapshot([_link_row("phone", "+6591234567", link_weight=None)])
    confidence, tx, _features = _score(
        tx=_StrictTx(), identifiers=[_phone_ident("+6591234567")], snapshot=snapshot
    )

    assert tx.queries == []
    assert round(confidence, 6) == 0.35


def test_weighted_conversation_cap_uses_attach_band() -> None:
    identifiers = [_phone_ident("+6591234567"), _email_ident("user@example.com")]
    rows = [
        _link_row("phone", "+6591234567", system_sourced=True),
        _link_row("email", "user@example.com", system_sourced=True),
    ]
    legacy_confidence, _, _ = _score(
        _FanoutTx(),
        identifiers,
        _snapshot(rows),
        record_type=RecordType.CONVERSATION,
        weighted=False,
    )
    weighted_confidence, _, _ = _score(
        _StrictTx(),
        identifiers,
        _snapshot(rows),
        record_type=RecordType.CONVERSATION,
        weighted=True,
    )

    assert round(legacy_confidence, 6) == 0.39
    assert round(weighted_confidence, 6) == 0.69


def test_high_participant_count_becomes_veto_not_penalty() -> None:
    snapshot = _snapshot(
        [_link_row("phone", "+6591234567", link_weight=1.0, participant_count=100)]
    )
    _confidence, tx, features = _score(_StrictTx(), [_phone_ident("+6591234567")], snapshot)

    assert tx.queries == []
    assert features["phone_high_fanout"] is True


def test_idf_discounts_widely_shared_identifiers() -> None:
    assert idf(None) == 1.0
    assert idf(0) == 1.0
    assert idf(1) == 1.0
    assert idf(7) == pytest.approx(1.0 / (1.0 + math.log(7)))
    assert idf(1) > idf(7) > idf(100)


def test_weighted_signal_combines_signal_weight_and_idf() -> None:
    assert weighted_signal(0.35, None, None) == pytest.approx(0.35)
    assert weighted_signal(0.35, 0.5, 1) == pytest.approx(0.175)
    assert weighted_signal(0.35, 1.0, 7) == pytest.approx(0.35 * idf(7))
