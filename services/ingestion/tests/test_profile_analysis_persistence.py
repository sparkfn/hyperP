"""Contracts for immutable ProfileAnalysis attempts and current pointers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError
from src.graph import queries
from src.profile_analysis_models import (
    ProfileAnalysisAttempt,
    ProfileAnalysisStatus,
    ProfileAnalysisType,
)

type AttemptInput = (
    UUID | datetime | ProfileAnalysisStatus | ProfileAnalysisType | bool | int | str | None
)


def _attempt(**changes: AttemptInput) -> ProfileAnalysisAttempt:
    values: dict[str, AttemptInput] = {
        "analysis_id": UUID("12345678-1234-5678-1234-567812345678"),
        "person_id": "person-1",
        "analysis_type": ProfileAnalysisType.SALES,
        "status": ProfileAnalysisStatus.SUCCEEDED,
        "content": "Supported sales analysis.",
        "input_revision": 7,
        "input_fingerprint": "sha256:abc",
        "prompt_version": "sales-profile-v1",
        "provider": "proclaude",
        "model": "profile-model",
        "started_at": datetime(2026, 7, 21, 1, tzinfo=UTC),
        "completed_at": datetime(2026, 7, 21, 1, 1, tzinfo=UTC),
        "attempt_number": 1,
    }
    values.update(changes)
    return ProfileAnalysisAttempt.model_validate(values)


def test_profile_analysis_enums_are_closed() -> None:
    assert [analysis_type.value for analysis_type in ProfileAnalysisType] == [
        "sales",
        "contact_tracing",
    ]
    assert [status.value for status in ProfileAnalysisStatus] == [
        "succeeded",
        "failed",
        "obsolete",
    ]


@pytest.mark.parametrize("analysis_type", list(ProfileAnalysisType))
def test_profile_analysis_attempt_accepts_each_analysis_type(
    analysis_type: ProfileAnalysisType,
) -> None:
    assert _attempt(analysis_type=analysis_type).analysis_type is analysis_type


@pytest.mark.parametrize("field", ["analysis_type", "status"])
def test_profile_analysis_attempt_rejects_unknown_enum_values(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        _attempt(**{field: "unknown"})


def test_successful_attempt_requires_content_without_failure_metadata() -> None:
    assert _attempt().content == "Supported sales analysis."

    with pytest.raises(ValidationError, match="content"):
        _attempt(content=None)
    with pytest.raises(ValidationError, match="failure metadata"):
        _attempt(failure_code="provider_error", retryable=True)


def test_failed_attempt_has_no_content_and_carries_safe_failure_metadata() -> None:
    attempt = _attempt(
        status=ProfileAnalysisStatus.FAILED,
        content=None,
        failure_code="provider_unavailable",
        retryable=True,
        next_retry_at=datetime(2026, 7, 21, 1, 6, tzinfo=UTC),
    )

    assert attempt.failure_code == "provider_unavailable"
    assert attempt.retryable is True
    non_retryable = _attempt(
        status=ProfileAnalysisStatus.FAILED,
        content=None,
        failure_code="invalid_output",
        retryable=False,
    )
    assert non_retryable.next_retry_at is None
    with pytest.raises(ValidationError, match="content"):
        _attempt(status=ProfileAnalysisStatus.FAILED)
    with pytest.raises(ValidationError, match="failure_code"):
        _attempt(status=ProfileAnalysisStatus.FAILED, content=None)
    with pytest.raises(ValidationError, match="retryable"):
        _attempt(
            status=ProfileAnalysisStatus.FAILED,
            content=None,
            failure_code="provider_unavailable",
        )
    with pytest.raises(ValidationError, match="next_retry_at"):
        _attempt(
            status=ProfileAnalysisStatus.FAILED,
            content=None,
            failure_code="provider_unavailable",
            retryable=True,
        )
    with pytest.raises(ValidationError, match="next_retry_at"):
        _attempt(
            status=ProfileAnalysisStatus.FAILED,
            content=None,
            failure_code="provider_unavailable",
            retryable=False,
            next_retry_at=datetime(2026, 7, 21, 1, 6, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "failure_code",
    (
        "",
        "Provider said: upstream response body",
        "PROVIDER_UNAVAILABLE",
        "provider-unavailable",
        "provider__unavailable",
        "x" * 65,
    ),
)
def test_failed_attempt_rejects_unsafe_failure_codes(failure_code: str) -> None:
    with pytest.raises(ValidationError, match="failure_code"):
        _attempt(
            status=ProfileAnalysisStatus.FAILED,
            content=None,
            failure_code=failure_code,
            retryable=False,
        )


def test_obsolete_attempt_has_neither_content_nor_failure_metadata() -> None:
    attempt = _attempt(status=ProfileAnalysisStatus.OBSOLETE, content=None)

    assert attempt.content is None
    with pytest.raises(ValidationError, match="failure metadata"):
        _attempt(
            status=ProfileAnalysisStatus.OBSOLETE,
            content=None,
            failure_code="stale_revision",
            retryable=False,
        )


def test_attempt_provenance_rejects_invalid_revision_and_timestamps() -> None:
    with pytest.raises(ValidationError, match="input_revision"):
        _attempt(input_revision=-1)
    with pytest.raises(ValidationError, match="completed_at"):
        _attempt(completed_at=datetime(2026, 7, 21, 0, 59, tzinfo=UTC))


def test_attempt_cypher_parameters_serialize_uuid_and_enums_but_not_datetimes() -> None:
    attempt = _attempt()

    parameters = attempt.to_cypher_parameters()

    assert parameters["analysis_id"] == "12345678-1234-5678-1234-567812345678"
    assert type(parameters["analysis_id"]) is str
    assert parameters["analysis_type"] == "sales"
    assert type(parameters["analysis_type"]) is str
    assert parameters["status"] == "succeeded"
    assert type(parameters["status"]) is str
    assert parameters["started_at"] is attempt.started_at
    assert parameters["completed_at"] is attempt.completed_at
    assert isinstance(parameters["started_at"], datetime)
    assert isinstance(parameters["completed_at"], datetime)


def test_profile_analysis_queries_are_exported() -> None:
    assert queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT
    assert queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS


def test_non_success_attempt_persistence_creates_immutable_history_only() -> None:
    query = queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT

    assert "WHERE $status IN ['failed', 'obsolete']" in query
    assert "CREATE (analysis:ProfileAnalysis" in query
    assert "CREATE (person)-[:HAS_PROFILE_ANALYSIS]->(analysis)" in query
    assert "MERGE (analysis:ProfileAnalysis" not in query
    assert "CURRENT_PROFILE_ANALYSIS" not in query
    assert "content: $content" not in query
    assert "failure_code: CASE WHEN actual_status = 'failed'" in query
    assert "$analysis_id" in query
    assert "$person_id" in query
    assert "$analysis_type" in query


def test_success_persistence_guards_publication_and_retains_obsolete_history() -> None:
    query = queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS

    assert "person.status = 'active'" in query
    assert "coalesce(person.analysis_input_revision, 0) = $input_revision" in query
    assert "CASE WHEN publishable THEN 'succeeded' ELSE 'obsolete' END" in query
    assert "content: $content" in query
    assert "CREATE (person)-[:HAS_PROFILE_ANALYSIS]->(analysis)" in query


@pytest.mark.parametrize(
    "query",
    (
        queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT,
        queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS,
    ),
)
def test_attempt_persistence_validates_claim_after_person_lock(query: str) -> None:
    lock = "SET person.analysis_last_attempt_at = CASE"
    token_guard = "person.analysis_claim_token = $claim_token"
    lease_guard = "person.analysis_claim_until > datetime.realtime()"
    create = "CREATE (analysis:ProfileAnalysis"

    assert query.index(lock) < query.index(token_guard) < query.index(create)
    assert query.index(lock) < query.index(lease_guard) < query.index(create)
    assert "person.analysis_claim_until > datetime()" not in query
    assert "person.analysis_claim_until > $completed_at" not in query


def test_stale_failed_attempt_becomes_obsolete_without_retry_metadata() -> None:
    query = queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT

    assert "CASE WHEN claim_owned THEN $status ELSE 'obsolete' END AS actual_status" in query
    assert "person.status = 'active'" in query
    assert "coalesce(person.analysis_input_revision, 0) = $input_revision" in query
    assert "status: actual_status" in query
    assert "CASE WHEN actual_status = 'failed' THEN $failure_code ELSE null END" in query
    assert "CASE WHEN actual_status = 'failed' THEN $retryable ELSE null END" in query
    assert "CASE WHEN actual_status = 'failed' THEN $next_retry_at ELSE null END" in query


@pytest.mark.parametrize(
    "query",
    (
        queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT,
        queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS,
    ),
)
def test_attempt_persistence_locks_person_with_monotonic_last_attempt(query: str) -> None:
    lock_write = "SET person.analysis_last_attempt_at = CASE"
    history_create = "CREATE (analysis:ProfileAnalysis"

    assert lock_write in query
    assert "WHEN person.analysis_last_attempt_at IS NULL" in query
    assert "OR person.analysis_last_attempt_at < $completed_at" in query
    assert "THEN $completed_at" in query
    assert "ELSE person.analysis_last_attempt_at" in query
    assert query.index(lock_write) < query.index(history_create)


def test_success_publish_locks_person_before_guard_and_pointer_replacement() -> None:
    """The Person write serializes same-type publishers before either reads current."""
    query = queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS
    lock_write = "SET person.analysis_last_attempt_at = CASE"
    status_guard = "person.status = 'active'"
    revision_guard = "coalesce(person.analysis_input_revision, 0) = $input_revision"
    pointer_match = "OPTIONAL MATCH (person)-[current:CURRENT_PROFILE_ANALYSIS"

    assert lock_write in query
    assert query.index(lock_write) < query.index(status_guard)
    assert query.index(lock_write) < query.index(revision_guard)
    assert query.index(lock_write) < query.index(pointer_match)


def test_success_publish_replaces_only_the_same_type_current_pointer() -> None:
    query = queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS
    pointer_match = "OPTIONAL MATCH (person)-[current:CURRENT_PROFILE_ANALYSIS"
    pointer_match_index = query.index(pointer_match)
    guard_index = query.index("WHERE publishable", pointer_match_index + len(pointer_match))
    delete_index = query.index("DELETE relationship", guard_index + len("WHERE publishable"))

    assert "[current:CURRENT_PROFILE_ANALYSIS {" in query
    assert "DELETE relationship" in query
    assert "CREATE (person)-[:CURRENT_PROFILE_ANALYSIS {" in query
    assert "analysis_type: $analysis_type" in query
    assert "CASE WHEN publishable THEN [1] ELSE [] END" in query
    assert pointer_match_index < guard_index < delete_index


def test_profile_analysis_queries_parameterize_persisted_values() -> None:
    persisted_parameters = (
        "analysis_id",
        "person_id",
        "analysis_type",
        "status",
        "content",
        "input_revision",
        "input_fingerprint",
        "prompt_version",
        "provider",
        "model",
        "started_at",
        "completed_at",
        "failure_code",
        "retryable",
        "next_retry_at",
        "attempt_number",
        "claim_token",
    )
    combined = "\n".join(
        (
            queries.PERSIST_PROFILE_ANALYSIS_ATTEMPT,
            queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS,
        )
    )

    for parameter in persisted_parameters:
        assert f"${parameter}" in combined


@dataclass(slots=True)
class _LeasePublicationState:
    claim_token: str
    claim_until: datetime
    status: str
    input_revision: int
    current_sales_analysis_id: str
    attempts: list[tuple[str, str]]

    def persist_success(
        self,
        query: str,
        *,
        worker_token: str,
        server_now: datetime,
        captured_revision: int,
        analysis_id: str,
    ) -> str:
        assert query is queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS
        claim_owned = self.claim_token == worker_token and self.claim_until > server_now
        publishable = (
            claim_owned and self.status == "active" and self.input_revision == captured_revision
        )
        attempt_status = "succeeded" if publishable else "obsolete"
        self.attempts.append((analysis_id, attempt_status))
        if publishable:
            self.current_sales_analysis_id = analysis_id
        return attempt_status


def test_expired_worker_cannot_replace_new_lease_owners_current_pointer() -> None:
    server_now = datetime(2026, 7, 21, 3, tzinfo=UTC)
    state = _LeasePublicationState(
        claim_token="worker-b",
        claim_until=server_now + timedelta(minutes=5),
        status="active",
        input_revision=7,
        current_sales_analysis_id="analysis-old",
        attempts=[],
    )

    assert (
        state.persist_success(
            queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS,
            worker_token="worker-b",
            server_now=server_now,
            captured_revision=7,
            analysis_id="analysis-b",
        )
        == "succeeded"
    )
    assert (
        state.persist_success(
            queries.PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS,
            worker_token="worker-a",
            server_now=server_now,
            captured_revision=7,
            analysis_id="analysis-a",
        )
        == "obsolete"
    )
    assert state.current_sales_analysis_id == "analysis-b"
    assert state.attempts == [
        ("analysis-b", "succeeded"),
        ("analysis-a", "obsolete"),
    ]
