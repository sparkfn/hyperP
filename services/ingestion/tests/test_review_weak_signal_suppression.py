"""Tests for default-off weak-signal review suppression and telemetry."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from typing import Literal, cast
from unittest.mock import MagicMock, patch

import pytest
from neo4j import ManagedTransaction

from src.config import Settings
from src.graph import queries
from src.models import (
    CandidateResult,
    EngineType,
    MatchDecision,
    MatchResult,
    RecordType,
    SourceRecordEnvelope,
    SourceRecordLifecycleStatus,
)
from src.pipeline import IngestPipeline
from src.pipeline_person_pairs import _open_review_case
from src.pipeline_writes import (
    REVIEW_CASES_CREATED_COUNTER,
    REVIEW_DECISIONS_SUPPRESSED_COUNTER,
    create_review_case_if_needed,
    reset_review_counters,
)

ReviewPolicy = Literal["off", "v2_weak_signal_park"]


class _Result:
    def __init__(self, row: dict[str, str] | None) -> None:
        self._row = row

    def single(self) -> dict[str, str] | None:
        return self._row


class _Transaction:
    def __init__(self, responses: dict[str, dict[str, str] | None] | None = None) -> None:
        self._responses = responses or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        return _Result(self._responses.get(query))


@pytest.fixture(autouse=True)
def _reset_counters() -> Iterator[None]:
    reset_review_counters()
    yield
    reset_review_counters()


def _set_review_policy(monkeypatch: pytest.MonkeyPatch, policy: ReviewPolicy) -> None:
    settings = Settings(neo4j_password="test", review_policy=policy)
    monkeypatch.setattr("src.pipeline_writes.get_settings", lambda: settings)


def _assert_today(day_utc: str) -> None:
    assert datetime.fromisoformat(day_utc).date().isoformat() == day_utc


def test_flag_off_preserves_review_case_write_and_default_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVIEW_POLICY", raising=False)
    assert Settings(neo4j_password="test", _env_file=None).review_policy == "off"
    _set_review_policy(monkeypatch, "off")
    tx = _Transaction({queries.CREATE_REVIEW_CASE: {"review_case_id": "review-1"}})
    result = MatchResult(
        decision=MatchDecision.REVIEW,
        engine_type=EngineType.HEURISTIC,
        reasons=["Phone match (unverified: +0.20)"],
    )

    assert (
        create_review_case_if_needed(
            cast(ManagedTransaction, tx),
            result,
            "decision-1",
            source_system="bitrix_chat",
        )
        == "review-1"
    )

    assert len(tx.calls) == 1
    query, parameters = tx.calls[0]
    assert query == queries.CREATE_REVIEW_CASE
    assert set(parameters) == {"match_decision_id", "priority", "sla_due_at"}
    assert parameters["match_decision_id"] == "decision-1"
    assert parameters["priority"] == 100
    assert isinstance(parameters["sla_due_at"], str)
    assert REVIEW_DECISIONS_SUPPRESSED_COUNTER == {}
    key = next(iter(REVIEW_CASES_CREATED_COUNTER))
    assert key[:3] == ("heuristic", "weak_signal", "bitrix_chat")
    _assert_today(key[3])
    assert REVIEW_CASES_CREATED_COUNTER[key] == 1


@pytest.mark.parametrize(
    ("reasons", "suppressed_reason"),
    [
        (["Phone match (unverified: +0.20)"], "unverified_phone"),
        (["Email match (unverified: +0.20)"], "unverified_email"),
        (["Phone near-match (NSN edit-distance=1: +0.10)"], "phone_near_match"),
        (
            ["Email near-match (domain/local-part near-miss: +0.10)"],
            "email_near_match",
        ),
        (
            ["Email match (unverified: +0.20)", "Phone match (unverified: +0.20)"],
            "weak_signal_combination",
        ),
    ],
)
def test_policy_suppresses_only_approved_heuristic_weak_reasons(
    monkeypatch: pytest.MonkeyPatch,
    reasons: list[str],
    suppressed_reason: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_review_policy(monkeypatch, "v2_weak_signal_park")
    tx = _Transaction(
        {
            queries.SUPPRESS_MATCH_DECISION_REVIEW: {
                "match_decision_id": "decision-1",
            }
        }
    )
    result = MatchResult(
        decision=MatchDecision.REVIEW,
        engine_type=EngineType.HEURISTIC,
        reasons=reasons,
    )

    with caplog.at_level(logging.INFO, logger="src.pipeline_writes"):
        assert (
            create_review_case_if_needed(
                cast(ManagedTransaction, tx),
                result,
                "decision-1",
                source_system="fundbox",
            )
            is None
        )

    assert tx.calls == [
        (
            queries.SUPPRESS_MATCH_DECISION_REVIEW,
            {
                "match_decision_id": "decision-1",
                "suppressed_reason": suppressed_reason,
            },
        )
    ]
    assert REVIEW_CASES_CREATED_COUNTER == {}
    key = next(iter(REVIEW_DECISIONS_SUPPRESSED_COUNTER))
    assert key[:2] == (suppressed_reason, "fundbox")
    _assert_today(key[2])
    assert REVIEW_DECISIONS_SUPPRESSED_COUNTER[key] == 1
    message = next(
        record.getMessage()
        for record in caplog.records
        if "event=review_decision_suppressed" in record.getMessage()
    )
    assert "match_decision_id=decision-1" in message
    assert f"suppressed_reason={suppressed_reason}" in message
    assert "source_system=fundbox" in message
    assert f"day_utc={key[2]}" in message
    assert all(reason not in message for reason in reasons)


def test_suppression_aborts_without_counting_if_match_decision_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_review_policy(monkeypatch, "v2_weak_signal_park")
    tx = _Transaction()
    result = MatchResult(
        decision=MatchDecision.REVIEW,
        engine_type=EngineType.HEURISTIC,
        reasons=["Phone match (unverified: +0.20)"],
    )

    with pytest.raises(RuntimeError, match="matched no MatchDecision for decision-1"):
        create_review_case_if_needed(
            cast(ManagedTransaction, tx),
            result,
            "decision-1",
            source_system="fundbox",
        )

    assert tx.calls == [
        (
            queries.SUPPRESS_MATCH_DECISION_REVIEW,
            {
                "match_decision_id": "decision-1",
                "suppressed_reason": "unverified_phone",
            },
        )
    ]
    assert REVIEW_DECISIONS_SUPPRESSED_COUNTER == {}
    assert REVIEW_CASES_CREATED_COUNTER == {}


@pytest.mark.parametrize(
    ("engine_type", "reasons", "candidate_ids", "expected_reason"),
    [
        (
            EngineType.DETERMINISTIC,
            ["Phone match (unverified: +0.20)"],
            [],
            "weak_signal",
        ),
        (
            EngineType.HEURISTIC,
            ["Phone match (unverified: +0.20)"],
            ["person-1", "person-2"],
            "crm_ambiguity",
        ),
        (EngineType.HEURISTIC, ["Ambiguous CRM deal contacts"], [], "crm_ambiguity"),
        (EngineType.HEURISTIC, ["High name similarity: 0.95"], [], "name_similarity"),
        (EngineType.HEURISTIC, ["Strong name mismatch"], [], "name_mismatch"),
        (EngineType.HEURISTIC, ["DOB exact match"], [], "dob_match"),
        (EngineType.HEURISTIC, ["DOB conflict: dates differ"], [], "dob_conflict"),
        (
            EngineType.HEURISTIC,
            ["Ambiguous prior_owners after update"],
            [],
            "continuity_reassignment",
        ),
        (EngineType.HEURISTIC, ["Phone match (verified: +0.80)"], [], "other"),
        (EngineType.HEURISTIC, ["unrecognized reason"], [], "other"),
    ],
)
def test_other_review_cases_remain_unchanged_and_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    engine_type: EngineType,
    reasons: list[str],
    candidate_ids: list[str],
    expected_reason: str,
) -> None:
    _set_review_policy(monkeypatch, "v2_weak_signal_park")
    tx = _Transaction({queries.CREATE_REVIEW_CASE: {"review_case_id": "review-1"}})
    result = MatchResult(
        decision=MatchDecision.REVIEW,
        engine_type=engine_type,
        reasons=reasons,
        review_candidate_person_ids=candidate_ids,
    )

    assert (
        create_review_case_if_needed(
            cast(ManagedTransaction, tx),
            result,
            "decision-1",
            source_system="source-a",
        )
        == "review-1"
    )

    assert len(tx.calls) == 1
    assert tx.calls[0][0] == queries.CREATE_REVIEW_CASE
    assert REVIEW_DECISIONS_SUPPRESSED_COUNTER == {}
    key = next(iter(REVIEW_CASES_CREATED_COUNTER))
    assert key[:3] == (engine_type.value, expected_reason, "source-a")
    _assert_today(key[3])
    assert REVIEW_CASES_CREATED_COUNTER[key] == 1


def test_pair_audit_case_records_real_match_decision_id_and_created_counter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tx = _Transaction(
        {
            queries.CREATE_PERSON_PAIR_REVIEW_CASE: {
                "review_case_id": "pair-review-1",
                "match_decision_id": "pair-decision-1",
            }
        }
    )

    with caplog.at_level(logging.INFO, logger="src.pipeline_writes"):
        result = _open_review_case(
            cast(ManagedTransaction, tx),
            left_person_id="person-a",
            right_person_id="person-b",
            confidence=0.25,
            reasons=["Shared phone links two active persons"],
            feature_snapshot="{}",
        )

    assert result is not None
    assert result.id == "pair-review-1"
    assert "md.match_decision_id AS match_decision_id" in tx.calls[0][0]
    key = next(iter(REVIEW_CASES_CREATED_COUNTER))
    assert key[:3] == ("pair_audit", "shared_identifier_bridge", "pair_audit_bridge")
    _assert_today(key[3])
    assert REVIEW_CASES_CREATED_COUNTER[key] == 1
    message = next(
        record.getMessage()
        for record in caplog.records
        if "event=review_case_created" in record.getMessage()
    )
    assert "review_case_id=pair-review-1" in message
    assert "match_decision_id=pair-decision-1" in message
    assert "engine_type=pair_audit" in message
    assert "reason_code=shared_identifier_bridge" in message
    assert "source_system=pair_audit_bridge" in message
    assert f"day_utc={key[3]}" in message


def test_pipeline_keeps_suppressed_review_record_pending_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_review_policy(monkeypatch, "v2_weak_signal_park")
    match_result = MatchResult(
        decision=MatchDecision.REVIEW,
        engine_type=EngineType.HEURISTIC,
        matched_person_id="person-1",
        reasons=["Phone match (unverified: +0.20)"],
    )
    pipeline = IngestPipeline(cast(object, MagicMock()))
    pipeline._match_engine = MagicMock(evaluate=MagicMock(return_value=match_result))
    envelope = SourceRecordEnvelope(
        source_system="fundbox",
        source_record_id="record-1",
        record_type=RecordType.IDENTITY,
        observed_at=None,
        record_hash="hash-1",
    )
    tx = _Transaction(
        {
            queries.SUPPRESS_MATCH_DECISION_REVIEW: {
                "match_decision_id": "decision-1",
            }
        }
    )

    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "src.pipeline.find_candidates", return_value=[CandidateResult(person_id="person-1")]
            )
        )
        persist = stack.enter_context(
            patch("src.pipeline.persist_source_record", return_value="source-record-1")
        )
        stack.enter_context(patch("src.pipeline.persist_match_decision", return_value="decision-1"))
        link = stack.enter_context(patch("src.pipeline.link_record_to_graph"))

        result = pipeline._execute_ingest(tx, envelope, [], [], [])

    assert result.match_decision is MatchDecision.REVIEW
    assert result.review_case_id is None
    assert result.match_decision_id == "decision-1"
    assert (
        persist.call_args.kwargs["lifecycle_status"] is SourceRecordLifecycleStatus.PENDING_REVIEW
    )
    assert link.call_args.kwargs["attach_evidence"] is False
    assert [query for query, _ in tx.calls] == [queries.SUPPRESS_MATCH_DECISION_REVIEW]
    assert REVIEW_CASES_CREATED_COUNTER == {}
    assert len(REVIEW_DECISIONS_SUPPRESSED_COUNTER) == 1
