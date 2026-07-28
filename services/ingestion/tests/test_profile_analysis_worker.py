"""Independent generation, validation, retry, and sweep contracts."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from itertools import count
from uuid import UUID

import httpx
import pytest
from src.llm import ChatMessage, get_profile_analysis_service
from src.profile_analysis_mapping import ProfileAnalysisTemporalMappingError
from src.profile_analysis_models import (
    ProfileAnalysisAttempt,
    ProfileAnalysisStatus,
    ProfileAnalysisType,
)
from src.profile_analysis_output import (
    ProfileAnalysisOutputReason,
    snapshot_evidence_references,
)
from src.profile_analysis_repository import (
    ClaimedProfileAnalysisBatch,
    ClaimedProfileAnalysisPerson,
    DueProfileAnalysis,
    ProfileAnalysisMappingError,
    ProfileAnalysisPersistenceResult,
    ProfileAnalysisSnapshotBundle,
)
from src.profile_analysis_snapshot import (
    ProfileSignalsInput,
    ProfileSnapshotInput,
    SnapshotOrderInput,
    SnapshotOrderItemInput,
    build_redacted_profile_snapshot,
)
from src.profile_analysis_snapshot_values import SafeSnapshotLabel
from src.profile_analysis_worker import (
    ProfileAnalysisOutputError,
    ProfileAnalysisPrivacyOutputError,
    ProfileAnalysisSweepSummary,
    run_profile_analysis_sweep,
    validate_profile_analysis_output,
)

_NOW = datetime(2026, 7, 21, 2, tzinfo=UTC)
_UUID_VALUES: Iterator[int] = count(1)


class _TextService:
    provider = "proclaude"
    default_model = "profile-model"

    def __init__(self, results: list[str | Exception]) -> None:
        self.results = results
        self.calls: list[list[ChatMessage]] = []

    def generate(self, messages: list[ChatMessage], *, max_tokens: int) -> str:
        assert max_tokens > 0
        self.calls.append(messages)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _Repository:
    def __init__(
        self,
        due: tuple[DueProfileAnalysis, ...],
        *,
        publication_statuses: list[ProfileAnalysisStatus] | None = None,
        fetch_error: Exception | None = None,
        known_sensitive_values: tuple[str, ...] = (),
        work_remaining: bool = False,
        renewal_results: list[bool] | None = None,
    ) -> None:
        self.due = due
        self.publication_statuses = publication_statuses or []
        self.fetch_error = fetch_error
        self.known_sensitive_values = known_sensitive_values
        self.work_remaining = work_remaining
        self.renewal_results = renewal_results or []
        self.attempts: list[ProfileAnalysisAttempt] = []
        self.persist_claim_tokens: list[str] = []
        self.claim_times: list[tuple[datetime, datetime]] = []
        self.renewals: list[tuple[str, int, str, datetime]] = []
        self.releases: list[tuple[str, str]] = []

    def claim_candidates(
        self,
        *,
        batch_size: int,
        claim_token: str,
        now: datetime,
        claim_until: datetime,
    ) -> ClaimedProfileAnalysisBatch:
        assert batch_size == 5
        assert claim_until > now
        self.claim_times.append((now, claim_until))
        return ClaimedProfileAnalysisBatch(
            people=(
                ClaimedProfileAnalysisPerson(person_id="person-1", input_revision=7, due=self.due),
            ),
            has_more=False,
        )

    def fetch_snapshot(self, person_id: str) -> ProfileAnalysisSnapshotBundle:
        assert person_id == "person-1"
        if self.fetch_error is not None:
            raise self.fetch_error
        snapshot = build_redacted_profile_snapshot(
            ProfileSnapshotInput(
                person_id=person_id,
                profile=ProfileSignalsInput(),
                orders=(
                    SnapshotOrderInput(
                        order_id="internal-order",
                        merchant=SafeSnapshotLabel("Workshop"),
                    ),
                ),
            )
        )
        return ProfileAnalysisSnapshotBundle(
            snapshot=snapshot,
            known_sensitive_values=self.known_sensitive_values,
        )

    def persist_attempt(
        self,
        attempt: ProfileAnalysisAttempt,
        *,
        claim_token: str,
    ) -> ProfileAnalysisPersistenceResult:
        self.attempts.append(attempt)
        self.persist_claim_tokens.append(claim_token)
        status = self.publication_statuses.pop(0) if self.publication_statuses else attempt.status
        return ProfileAnalysisPersistenceResult(
            status=status,
            published=status is ProfileAnalysisStatus.SUCCEEDED,
        )

    def release_claim(self, *, person_id: str, claim_token: str) -> bool:
        self.releases.append((person_id, claim_token))
        return True

    def renew_claim(
        self,
        *,
        person_id: str,
        input_revision: int,
        claim_token: str,
        claim_until: datetime,
    ) -> bool:
        self.renewals.append((person_id, input_revision, claim_token, claim_until))
        return self.renewal_results.pop(0) if self.renewal_results else True

    def has_eligible_work(self, *, now: datetime) -> bool:
        assert now == _NOW
        return self.work_remaining


def _clock() -> datetime:
    return _NOW


def _uuid_factory() -> UUID:
    return UUID(int=next(_UUID_VALUES))


def _due(*types: ProfileAnalysisType, attempt_number: int = 1) -> tuple[DueProfileAnalysis, ...]:
    return tuple(DueProfileAnalysis(item, attempt_number) for item in types)


def _run(
    repository: _Repository,
    service: _TextService,
) -> ProfileAnalysisSweepSummary:
    return run_profile_analysis_sweep(
        repository=repository,
        text_service=service,
        batch_size=5,
        claim_lease=timedelta(minutes=5),
        max_attempts=3,
        retry_base=timedelta(minutes=2),
        retry_cap=timedelta(hours=1),
        clock=_clock,
        uuid_factory=_uuid_factory,
        claim_token="claim-token",
    )


def test_profile_analysis_accessor_reuses_prose_singleton() -> None:
    assert get_profile_analysis_service() is get_profile_analysis_service()


@pytest.mark.parametrize(
    ("output", "reason"),
    (
        ("", ProfileAnalysisOutputReason.NOT_TRIMMED),
        (
            "```text\nSummary\n```\nLimitations: None.",
            ProfileAnalysisOutputReason.NOT_PLAIN_TEXT,
        ),
        (
            "~~~text\nSummary\n~~~\nLimitations: None.",
            ProfileAnalysisOutputReason.NOT_PLAIN_TEXT,
        ),
        ("<p>Summary</p>\nLimitations: None.", ProfileAnalysisOutputReason.NOT_PLAIN_TEXT),
        (
            '{"summary":"Activity","limitations":"Sparse evidence"}',
            ProfileAnalysisOutputReason.NOT_PLAIN_TEXT,
        ),
        (
            "Summary without a limitations section.",
            ProfileAnalysisOutputReason.MISSING_LIMITATIONS,
        ),
        (
            "Unsupported evidence source-99.\nLimitations: None.",
            ProfileAnalysisOutputReason.UNKNOWN_EVIDENCE,
        ),
        ("word " * 351 + "\nLimitations: None.", ProfileAnalysisOutputReason.TOO_LARGE),
    ),
)
def test_plain_text_output_validation_is_bounded_and_evidence_local(
    output: str,
    reason: ProfileAnalysisOutputReason,
) -> None:
    with pytest.raises(ProfileAnalysisOutputError) as exc_info:
        validate_profile_analysis_output(output, frozenset({"order-1"}), ())

    assert exc_info.value.reason is reason


def test_plain_text_output_validation_accepts_supported_local_references() -> None:
    output = "Observed workshop activity (order-1).\nLimitations: Dates are incomplete."

    assert validate_profile_analysis_output(output, frozenset({"order-1"}), ()) == output


def test_json_output_has_a_safe_structured_validation_reason() -> None:
    output = '{"summary":"DIRECT-SECRET","limitations":"Sparse evidence"}'

    with pytest.raises(ProfileAnalysisOutputError) as exc_info:
        validate_profile_analysis_output(output, frozenset(), ("DIRECT-SECRET",))

    assert exc_info.value.reason is ProfileAnalysisOutputReason.NOT_PLAIN_TEXT
    assert "DIRECT-SECRET" not in str(exc_info.value)


def test_malformed_object_shaped_output_is_not_publishable() -> None:
    output = '{"summary": "Activity"\nLimitations: Sparse evidence.}'

    with pytest.raises(ProfileAnalysisOutputError) as exc_info:
        validate_profile_analysis_output(output, frozenset(), ())

    assert exc_info.value.reason is ProfileAnalysisOutputReason.NOT_PLAIN_TEXT


def test_malformed_array_shaped_output_is_not_publishable() -> None:
    output = '["Activity"\nLimitations: Sparse evidence.]'

    with pytest.raises(ProfileAnalysisOutputError) as exc_info:
        validate_profile_analysis_output(output, frozenset(), ())

    assert exc_info.value.reason is ProfileAnalysisOutputReason.NOT_PLAIN_TEXT


def test_deeply_nested_json_output_is_rejected_without_escaping_validation() -> None:
    output = "[" * 1_100 + "]" * 1_100

    with pytest.raises(ProfileAnalysisOutputError) as exc_info:
        validate_profile_analysis_output(output, frozenset(), ())

    assert exc_info.value.reason is ProfileAnalysisOutputReason.NOT_PLAIN_TEXT


@pytest.mark.parametrize(
    ("output", "sensitive"),
    (
        ("Value 123.\nLimitations: Sparse.", 123),
        ("Value 2.5.\nLimitations: Sparse.", 2.5),
        ("Contact Li, noted.\nLimitations: Sparse.", "Li"),
    ),
)
def test_output_rejects_short_numeric_and_punctuated_sensitive_tokens(
    output: str,
    sensitive: int | float | str,
) -> None:
    with pytest.raises(ProfileAnalysisPrivacyOutputError):
        validate_profile_analysis_output(output, frozenset(), (sensitive,))


def test_evidence_reference_number_is_not_a_sensitive_numeric_token() -> None:
    output = "Observed activity (order-1).\nLimitations: Sparse."

    assert validate_profile_analysis_output(output, frozenset({"order-1"}), (1,)) == output


def test_output_rejects_long_sensitive_text_embedded_inside_a_word() -> None:
    output = "Marker XDIRECT-SECRET found.\nLimitations: Sparse."

    with pytest.raises(ProfileAnalysisPrivacyOutputError):
        validate_profile_analysis_output(output, frozenset(), ("DIRECT-SECRET",))


@pytest.mark.parametrize(
    ("output", "sensitive"),
    (
        ("Phone +65 9123-4567.\nLimitations: Sparse.", "+6591234567"),
        ("Postal 123 456.\nLimitations: Sparse.", "123456"),
    ),
)
def test_output_rejects_formatted_numeric_string_identifiers(
    output: str,
    sensitive: str,
) -> None:
    with pytest.raises(ProfileAnalysisPrivacyOutputError):
        validate_profile_analysis_output(output, frozenset(), (sensitive,))


def test_validated_evidence_reference_suffix_never_collides_with_sensitive_values() -> None:
    output = "Observed activity (order-123456).\nLimitations: Sparse."

    assert (
        validate_profile_analysis_output(
            output,
            frozenset({"order-123456"}),
            ("123456", 123456),
        )
        == output
    )


def test_evidence_allowlist_uses_typed_refs_not_reference_shaped_labels() -> None:
    snapshot = build_redacted_profile_snapshot(
        ProfileSnapshotInput(
            person_id="person-1",
            orders=(
                SnapshotOrderInput(
                    order_id="internal-order",
                    items=(SnapshotOrderItemInput(product=SafeSnapshotLabel("order-99")),),
                ),
            ),
        )
    )

    references = snapshot_evidence_references(snapshot)

    assert references == frozenset({"order-1"})


def test_sales_and_contact_calls_are_independent_and_partial_success_is_persisted() -> None:
    request = httpx.Request("POST", "https://provider.test")
    transport_failure = httpx.ConnectError("secret provider body", request=request)
    repository = _Repository(_due(ProfileAnalysisType.SALES, ProfileAnalysisType.CONTACT_TRACING))
    service = _TextService(
        [
            transport_failure,
            "Known relationship gaps.\nLimitations: No relationship events supplied.",
        ]
    )

    summary = _run(repository, service)

    assert len(service.calls) == 2
    assert [attempt.status for attempt in repository.attempts] == [
        ProfileAnalysisStatus.FAILED,
        ProfileAnalysisStatus.SUCCEEDED,
    ]
    assert repository.attempts[0].failure_code == "provider_unavailable"
    assert "secret" not in repository.attempts[0].failure_code
    assert summary["failed"] == 1
    assert summary["succeeded"] == 1
    assert repository.releases == [("person-1", "claim-token")]


def test_transient_failure_has_bounded_exponential_retry_metadata() -> None:
    request = httpx.Request("POST", "https://provider.test")
    response = httpx.Response(429, request=request, text="private provider response")
    error = httpx.HTTPStatusError("private provider response", request=request, response=response)
    repository = _Repository(_due(ProfileAnalysisType.SALES, attempt_number=2))

    _run(repository, _TextService([error]))

    attempt = repository.attempts[0]
    assert attempt.retryable is True
    assert attempt.next_retry_at == _NOW + timedelta(minutes=4)
    assert attempt.failure_code == "provider_rate_limited"
    assert "private" not in attempt.failure_code


def test_invalid_or_private_output_is_nonretryable_and_other_type_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES, ProfileAnalysisType.CONTACT_TRACING),
        known_sensitive_values=("DIRECT-SECRET",),
    )
    service = _TextService(
        [
            "Leaked DIRECT-SECRET.\nLimitations: None.",
            "No relationship events supplied.\nLimitations: Evidence is sparse.",
        ]
    )
    caplog.set_level("WARNING", logger="src.profile_analysis_worker")
    _run(repository, service)

    assert repository.attempts[0].status is ProfileAnalysisStatus.FAILED
    assert repository.attempts[0].failure_code == "privacy_output"
    assert repository.attempts[0].retryable is False
    assert repository.attempts[0].next_retry_at is None
    assert repository.attempts[1].status is ProfileAnalysisStatus.SUCCEEDED
    assert "reason=sensitive_value" in caplog.text
    assert "DIRECT-SECRET" not in caplog.text


def test_invalid_output_logs_only_the_safe_validation_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES),
        known_sensitive_values=("DIRECT-SECRET",),
    )
    caplog.set_level("WARNING", logger="src.profile_analysis_worker")

    _run(
        repository,
        _TextService(
            [
                '{"summary":"DIRECT-SECRET","limitations":"Sparse"}',
                '{"summary":"DIRECT-SECRET","limitations":"Sparse"}',
            ]
        ),
    )

    attempt = repository.attempts[0]
    assert attempt.failure_code == "invalid_output"
    assert attempt.retryable is False
    assert "reason=not_plain_text" in caplog.text
    assert "DIRECT-SECRET" not in caplog.text


def test_contract_only_output_failure_gets_one_bounded_safe_repair() -> None:
    repository = _Repository(_due(ProfileAnalysisType.SALES))
    service = _TextService(
        [
            "Observed workshop activity (order-1).",
            "Observed workshop activity (order-1).\nLimitations: Dates are incomplete.",
        ]
    )

    summary = _run(repository, service)

    assert summary["succeeded"] == 1
    assert repository.attempts[0].status is ProfileAnalysisStatus.SUCCEEDED
    assert len(service.calls) == 2
    repair_prompt = service.calls[1][0].content
    assert "safe reason 'missing_limitations'" in repair_prompt
    assert "Observed workshop activity (order-1)." not in repair_prompt


def test_contract_only_output_failure_stops_after_one_unsuccessful_repair() -> None:
    repository = _Repository(_due(ProfileAnalysisType.SALES))
    service = _TextService(
        [
            "Observed workshop activity (order-1).",
            "Still incomplete (order-1).",
            "Would be valid (order-1).\nLimitations: None.",
        ]
    )

    _run(repository, service)

    assert repository.attempts[0].failure_code == "invalid_output"
    assert len(service.calls) == 2
    assert len(service.results) == 1


@pytest.mark.parametrize(
    ("known_sensitive_value", "formatted_value"),
    (
        ("+6591234567", "+65 9123-4567"),
        ("S1234567A", "S 1234 567-A"),
        ("One Street #02-03", "One Street, 02 03"),
    ),
)
def test_output_rejects_formatted_sensitive_values(
    known_sensitive_value: str,
    formatted_value: str,
) -> None:
    output = f"Observed detail: {formatted_value}.\nLimitations: Evidence is sparse."

    with pytest.raises(ProfileAnalysisPrivacyOutputError):
        validate_profile_analysis_output(output, frozenset(), (known_sensitive_value,))


@pytest.mark.parametrize("formatted_dob", ("10-12-1985", "10/12/1985"))
def test_output_rejects_day_first_rendering_of_known_iso_dob(formatted_dob: str) -> None:
    output = f"Birth date {formatted_dob}.\nLimitations: Evidence is sparse."

    with pytest.raises(ProfileAnalysisPrivacyOutputError):
        validate_profile_analysis_output(output, frozenset(), ("1985-12-10",))


def test_output_allows_unrelated_day_first_order_date() -> None:
    output = "Order activity on 21/07/2026.\nLimitations: Evidence is sparse."

    assert validate_profile_analysis_output(output, frozenset(), ("1985-12-10",)) == output


def test_publication_race_is_counted_obsolete_and_does_not_clear_prior_current() -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES),
        publication_statuses=[ProfileAnalysisStatus.OBSOLETE],
    )
    service = _TextService(
        ["Observed workshop activity (order-1).\nLimitations: Dates are incomplete."]
    )

    summary = _run(repository, service)

    assert repository.attempts[0].status is ProfileAnalysisStatus.SUCCEEDED
    assert summary["obsolete"] == 1
    assert summary["succeeded"] == 0
    assert repository.releases == [("person-1", "claim-token")]


def test_claim_is_released_after_unexpected_snapshot_exception() -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES), fetch_error=RuntimeError("unexpected private data")
    )

    summary = _run(repository, _TextService([]))

    assert summary["unexpected_failures"] == 1
    assert repository.releases == [("person-1", "claim-token")]


def test_sweep_rechecks_work_after_releasing_a_failed_claim() -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES),
        fetch_error=RuntimeError("unexpected private data"),
        work_remaining=True,
    )

    summary = _run(repository, _TextService([]))

    assert repository.releases == [("person-1", "claim-token")]
    assert summary["has_more"] is True


def test_invalid_snapshot_persists_nonretryable_failure_for_each_due_type() -> None:
    due = (
        DueProfileAnalysis(ProfileAnalysisType.SALES, 2),
        DueProfileAnalysis(ProfileAnalysisType.CONTACT_TRACING, 3),
    )
    repository = _Repository(
        due,
        fetch_error=ProfileAnalysisMappingError("private malformed value"),
    )
    service = _TextService([])

    summary = _run(repository, service)

    sentinel = "sha256:" + hashlib.sha256(b"profile-analysis-invalid-snapshot-v1").hexdigest()
    assert [attempt.analysis_type for attempt in repository.attempts] == [
        ProfileAnalysisType.SALES,
        ProfileAnalysisType.CONTACT_TRACING,
    ]
    assert [attempt.attempt_number for attempt in repository.attempts] == [2, 3]
    assert all(attempt.status is ProfileAnalysisStatus.FAILED for attempt in repository.attempts)
    assert all(attempt.failure_code == "invalid_snapshot" for attempt in repository.attempts)
    assert all(attempt.retryable is False for attempt in repository.attempts)
    assert all(attempt.next_retry_at is None for attempt in repository.attempts)
    assert all(attempt.input_fingerprint == sentinel for attempt in repository.attempts)
    assert [attempt.prompt_version for attempt in repository.attempts] == [
        "sales-profile-v2",
        "contact-tracing-profile-v2",
    ]
    assert all(attempt.provider == "proclaude" for attempt in repository.attempts)
    assert all(attempt.model == "profile-model" for attempt in repository.attempts)
    assert all(attempt.input_revision == 7 for attempt in repository.attempts)
    assert all(attempt.started_at == _NOW for attempt in repository.attempts)
    assert all(attempt.completed_at == _NOW for attempt in repository.attempts)
    assert all(attempt.content is None for attempt in repository.attempts)
    assert "private malformed value" not in repr(repository.attempts)
    assert service.calls == []
    assert summary["attempted"] == 2
    assert summary["failed"] == 2
    assert summary["unexpected_failures"] == 0
    assert repository.releases == [("person-1", "claim-token")]


def test_invalid_snapshot_temporal_data_has_a_distinct_safe_failure_code() -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES, ProfileAnalysisType.CONTACT_TRACING),
        fetch_error=ProfileAnalysisTemporalMappingError("private malformed timestamp"),
    )

    summary = _run(repository, _TextService([]))

    assert [attempt.failure_code for attempt in repository.attempts] == [
        "invalid_snapshot_temporal",
        "invalid_snapshot_temporal",
    ]
    assert all(attempt.retryable is False for attempt in repository.attempts)
    assert "private malformed timestamp" not in repr(repository.attempts)
    assert summary["failed"] == 2
    assert summary["unexpected_failures"] == 0


class _PartialInvalidSnapshotRepository(_Repository):
    def persist_attempt(
        self,
        attempt: ProfileAnalysisAttempt,
        *,
        claim_token: str,
    ) -> ProfileAnalysisPersistenceResult:
        if attempt.analysis_type is ProfileAnalysisType.SALES:
            raise RuntimeError("private database error")
        return super().persist_attempt(attempt, claim_token=claim_token)


def test_invalid_snapshot_attempts_each_type_after_one_persistence_failure() -> None:
    repository = _PartialInvalidSnapshotRepository(
        _due(ProfileAnalysisType.SALES, ProfileAnalysisType.CONTACT_TRACING),
        fetch_error=ProfileAnalysisMappingError("private malformed value"),
    )

    summary = _run(repository, _TextService([]))

    assert [attempt.analysis_type for attempt in repository.attempts] == [
        ProfileAnalysisType.CONTACT_TRACING
    ]
    assert summary["attempted"] == 2
    assert summary["failed"] == 1
    assert summary["unexpected_failures"] == 1
    assert repository.releases == [("person-1", "claim-token")]


def test_sweep_summary_is_typed_and_reports_remaining_work() -> None:
    repository = _Repository(_due(ProfileAnalysisType.SALES))
    service = _TextService(
        ["Observed workshop activity (order-1).\nLimitations: Dates are incomplete."]
    )

    summary = _run(repository, service)

    assert summary == {
        "claimed": 1,
        "attempted": 1,
        "succeeded": 1,
        "failed": 0,
        "obsolete": 0,
        "unexpected_failures": 0,
        "released": 1,
        "has_more": False,
    }
    assert repository.persist_claim_tokens == ["claim-token"]


def test_sweep_renews_batch_lease_before_snapshot_and_after_provider_call() -> None:
    times = iter(
        (
            _NOW,
            _NOW,
            _NOW,
            _NOW + timedelta(minutes=6),
            _NOW + timedelta(minutes=6),
            _NOW + timedelta(minutes=6),
        )
    )
    repository = _Repository(_due(ProfileAnalysisType.SALES))
    service = _TextService(
        ["Observed workshop activity (order-1).\nLimitations: Dates are incomplete."]
    )

    run_profile_analysis_sweep(
        repository=repository,
        text_service=service,
        batch_size=5,
        claim_lease=timedelta(minutes=5),
        max_attempts=3,
        retry_base=timedelta(minutes=2),
        retry_cap=timedelta(hours=1),
        clock=lambda: next(times),
        uuid_factory=_uuid_factory,
        claim_token="claim-token",
    )

    assert [renewal[3] for renewal in repository.renewals] == [
        _NOW + timedelta(minutes=5),
        _NOW + timedelta(minutes=11),
    ]
    assert repository.attempts[0].status is ProfileAnalysisStatus.SUCCEEDED


def test_lost_lease_stops_before_calling_the_second_analysis_type() -> None:
    repository = _Repository(
        _due(ProfileAnalysisType.SALES, ProfileAnalysisType.CONTACT_TRACING),
        publication_statuses=[ProfileAnalysisStatus.OBSOLETE],
        renewal_results=[True, False],
    )
    service = _TextService(
        ["Observed workshop activity (order-1).\nLimitations: Dates are incomplete."]
    )

    summary = _run(repository, service)

    assert len(service.calls) == 1
    assert [attempt.analysis_type for attempt in repository.attempts] == [ProfileAnalysisType.SALES]
    assert summary["attempted"] == 1
    assert summary["obsolete"] == 1


def test_naive_clock_is_rejected_before_claiming() -> None:
    repository = _Repository(_due(ProfileAnalysisType.SALES))

    with pytest.raises(ValueError, match="timezone-aware"):
        run_profile_analysis_sweep(
            repository=repository,
            text_service=_TextService([]),
            batch_size=5,
            claim_lease=timedelta(minutes=5),
            max_attempts=3,
            retry_base=timedelta(minutes=2),
            retry_cap=timedelta(hours=1),
            clock=lambda: datetime(2026, 7, 21, 2),
            claim_token="claim-token",
        )

    assert repository.claim_times == []


def test_offset_aware_clock_is_normalized_to_utc_everywhere() -> None:
    offset_time = datetime(2026, 7, 21, 10, tzinfo=timezone(timedelta(hours=8)))
    repository = _Repository(_due(ProfileAnalysisType.SALES))
    service = _TextService(["Observed workshop activity (order-1).\nLimitations: Sparse."])

    run_profile_analysis_sweep(
        repository=repository,
        text_service=service,
        batch_size=5,
        claim_lease=timedelta(minutes=5),
        max_attempts=3,
        retry_base=timedelta(minutes=2),
        retry_cap=timedelta(hours=1),
        clock=lambda: offset_time,
        claim_token="claim-token",
    )

    assert repository.claim_times == [
        (
            datetime(2026, 7, 21, 2, tzinfo=UTC),
            datetime(2026, 7, 21, 2, 5, tzinfo=UTC),
        )
    ]
    assert repository.attempts[0].started_at == datetime(2026, 7, 21, 2, tzinfo=UTC)
    assert repository.attempts[0].completed_at == datetime(2026, 7, 21, 2, tzinfo=UTC)
