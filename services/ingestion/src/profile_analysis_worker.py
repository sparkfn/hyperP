"""Synchronous bounded sweep for independent Person profile analyses."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx

from src.llm import ChatMessage
from src.profile_analysis_models import (
    ProfileAnalysisAttempt,
    ProfileAnalysisStatus,
    ProfileAnalysisType,
)
from src.profile_analysis_output import (
    ProfileAnalysisOutputError,
    ProfileAnalysisPrivacyOutputError,
    snapshot_evidence_references,
    validate_profile_analysis_output,
)
from src.profile_analysis_prompts import (
    CONTACT_TRACING_PROFILE_PROMPT_VERSION,
    SALES_PROFILE_PROMPT_VERSION,
    build_contact_tracing_profile_messages,
    build_sales_profile_messages,
)
from src.profile_analysis_repository import (
    ClaimedProfileAnalysisPerson,
    DueProfileAnalysis,
    ProfileAnalysisMappingError,
    ProfileAnalysisRepository,
    ProfileAnalysisSnapshotBundle,
)
from src.profile_analysis_snapshot import (
    ProfileAnalysisPrivacyError,
    snapshot_fingerprint,
)
from src.profile_analysis_worker_types import (
    ProfileAnalysisAttemptContext,
    ProfileAnalysisRetryPolicy,
    ProfileAnalysisSweepCounts,
    ProfileAnalysisSweepSummary,
    ProfileAnalysisTextService,
    profile_analysis_clock_utc,
)

_MAX_GENERATION_TOKENS = 700
_INVALID_SNAPSHOT_FINGERPRINT = (
    "sha256:" + hashlib.sha256(b"profile-analysis-invalid-snapshot-v1").hexdigest()
)


def run_profile_analysis_sweep(
    *,
    repository: ProfileAnalysisRepository,
    text_service: ProfileAnalysisTextService,
    batch_size: int,
    claim_lease: timedelta,
    max_attempts: int,
    retry_base: timedelta,
    retry_cap: timedelta,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID] = uuid4,
    claim_token: str | None = None,
) -> ProfileAnalysisSweepSummary:
    """Claim and process one bounded batch without holding graph transactions."""
    _validate_settings(batch_size, claim_lease, max_attempts, retry_base, retry_cap)
    token = claim_token or str(uuid_factory())
    claimed_at = profile_analysis_clock_utc(clock)
    batch = repository.claim_candidates(
        batch_size=batch_size,
        claim_token=token,
        now=claimed_at,
        claim_until=claimed_at + claim_lease,
    )
    counts = ProfileAnalysisSweepCounts()
    retry_policy = ProfileAnalysisRetryPolicy(max_attempts, retry_base, retry_cap)
    for person in batch.people:
        try:
            _process_person(
                repository=repository,
                text_service=text_service,
                person=person,
                retry_policy=retry_policy,
                clock=clock,
                uuid_factory=uuid_factory,
                counts=counts,
                claim_token=token,
                claim_lease=claim_lease,
            )
        except Exception:
            counts.unexpected_failures += 1
        finally:
            try:
                if repository.release_claim(person_id=person.person_id, claim_token=token):
                    counts.released += 1
            except Exception:
                counts.unexpected_failures += 1
    has_more = batch.has_more
    try:
        has_more = repository.has_eligible_work(now=profile_analysis_clock_utc(clock))
    except Exception:
        counts.unexpected_failures += 1
    return _summary(has_more, len(batch.people), counts)


def _summary(
    has_more: bool,
    claimed: int,
    counts: ProfileAnalysisSweepCounts,
) -> ProfileAnalysisSweepSummary:
    return ProfileAnalysisSweepSummary(
        claimed=claimed,
        attempted=counts.attempted,
        succeeded=counts.succeeded,
        failed=counts.failed,
        obsolete=counts.obsolete,
        unexpected_failures=counts.unexpected_failures,
        released=counts.released,
        has_more=has_more,
    )


def _process_person(
    *,
    repository: ProfileAnalysisRepository,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    retry_policy: ProfileAnalysisRetryPolicy,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
    counts: ProfileAnalysisSweepCounts,
    claim_token: str,
    claim_lease: timedelta,
) -> None:
    if not _renew_claim(
        repository=repository,
        person=person,
        claim_token=claim_token,
        claim_lease=claim_lease,
        clock=clock,
        counts=counts,
    ):
        return
    try:
        bundle = repository.fetch_snapshot(person.person_id)
    except ProfileAnalysisMappingError:
        _persist_invalid_snapshot_attempts(
            repository=repository,
            text_service=text_service,
            person=person,
            retry_policy=retry_policy,
            clock=clock,
            uuid_factory=uuid_factory,
            counts=counts,
            claim_token=claim_token,
            claim_lease=claim_lease,
        )
        return
    fingerprint = snapshot_fingerprint(bundle.snapshot)
    evidence = snapshot_evidence_references(bundle.snapshot)
    for due in person.due:
        counts.attempted += 1
        try:
            attempt = _generate_attempt(
                text_service=text_service,
                person=person,
                due=due,
                bundle=bundle,
                fingerprint=fingerprint,
                evidence=evidence,
                retry_policy=retry_policy,
                clock=clock,
                uuid_factory=uuid_factory,
            )
            renewed = _renew_claim(
                repository=repository,
                person=person,
                claim_token=claim_token,
                claim_lease=claim_lease,
                clock=clock,
                counts=counts,
            )
            counts.record(repository.persist_attempt(attempt, claim_token=claim_token))
            if not renewed:
                return
        except Exception:
            counts.unexpected_failures += 1


def _generate_attempt(
    *,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    due: DueProfileAnalysis,
    bundle: ProfileAnalysisSnapshotBundle,
    fingerprint: str,
    evidence: frozenset[str],
    retry_policy: ProfileAnalysisRetryPolicy,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
) -> ProfileAnalysisAttempt:
    context = _attempt_context(
        text_service=text_service,
        person=person,
        due=due,
        fingerprint=fingerprint,
        retry_policy=retry_policy,
        clock=clock,
        uuid_factory=uuid_factory,
    )
    try:
        messages = _messages(due.analysis_type, bundle)
        output = _call_provider(text_service, messages)
        content = validate_profile_analysis_output(
            output,
            evidence,
            bundle.known_sensitive_values,
        )
    except ProfileAnalysisPrivacyError:
        return _failed_attempt(context, "privacy_snapshot", False)
    except ProfileAnalysisPrivacyOutputError:
        return _failed_attempt(context, "privacy_output", False)
    except ProfileAnalysisOutputError:
        return _failed_attempt(context, "invalid_output", False)
    except httpx.HTTPStatusError as error:
        code, retryable = _http_failure(error)
        return _failed_attempt(context, code, retryable)
    except (httpx.TimeoutException, httpx.TransportError):
        return _failed_attempt(context, "provider_unavailable", True)
    return _succeeded_attempt(context, content)


def _persist_invalid_snapshot_attempts(
    *,
    repository: ProfileAnalysisRepository,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    retry_policy: ProfileAnalysisRetryPolicy,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
    counts: ProfileAnalysisSweepCounts,
    claim_token: str,
    claim_lease: timedelta,
) -> None:
    for due in person.due:
        if not _renew_claim(
            repository=repository,
            person=person,
            claim_token=claim_token,
            claim_lease=claim_lease,
            clock=clock,
            counts=counts,
        ):
            return
        counts.attempted += 1
        context = _attempt_context(
            text_service=text_service,
            person=person,
            due=due,
            fingerprint=_INVALID_SNAPSHOT_FINGERPRINT,
            retry_policy=retry_policy,
            clock=clock,
            uuid_factory=uuid_factory,
        )
        try:
            counts.record(
                repository.persist_attempt(
                    _failed_attempt(context, "invalid_snapshot", False),
                    claim_token=claim_token,
                )
            )
        except Exception:
            counts.unexpected_failures += 1


def _renew_claim(
    *,
    repository: ProfileAnalysisRepository,
    person: ClaimedProfileAnalysisPerson,
    claim_token: str,
    claim_lease: timedelta,
    clock: Callable[[], datetime],
    counts: ProfileAnalysisSweepCounts,
) -> bool:
    try:
        now = profile_analysis_clock_utc(clock)
        return repository.renew_claim(
            person_id=person.person_id,
            input_revision=person.input_revision,
            claim_token=claim_token,
            claim_until=now + claim_lease,
        )
    except Exception:
        counts.unexpected_failures += 1
        return False


def _attempt_context(
    *,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    due: DueProfileAnalysis,
    fingerprint: str,
    retry_policy: ProfileAnalysisRetryPolicy,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
) -> ProfileAnalysisAttemptContext:
    return ProfileAnalysisAttemptContext(
        person=person,
        due=due,
        fingerprint=fingerprint,
        prompt_version=_prompt_version(due.analysis_type),
        text_service=text_service,
        started_at=profile_analysis_clock_utc(clock),
        retry_policy=retry_policy,
        clock=clock,
        uuid_factory=uuid_factory,
    )


def _succeeded_attempt(
    context: ProfileAnalysisAttemptContext,
    content: str,
) -> ProfileAnalysisAttempt:
    return ProfileAnalysisAttempt(
        analysis_id=context.uuid_factory(),
        person_id=context.person.person_id,
        analysis_type=context.due.analysis_type,
        status=ProfileAnalysisStatus.SUCCEEDED,
        content=content,
        input_revision=context.person.input_revision,
        input_fingerprint=context.fingerprint,
        prompt_version=context.prompt_version,
        provider=context.text_service.provider,
        model=context.text_service.default_model,
        started_at=context.started_at,
        completed_at=profile_analysis_clock_utc(context.clock),
        attempt_number=context.due.attempt_number,
    )


def _messages(
    analysis_type: ProfileAnalysisType,
    bundle: ProfileAnalysisSnapshotBundle,
) -> list[ChatMessage]:
    builder = (
        build_sales_profile_messages
        if analysis_type is ProfileAnalysisType.SALES
        else build_contact_tracing_profile_messages
    )
    return builder(
        bundle.snapshot,
        known_sensitive_values=bundle.known_sensitive_values,
    )


def _call_provider(
    text_service: ProfileAnalysisTextService,
    messages: list[ChatMessage],
) -> str:
    try:
        return text_service.generate(messages, max_tokens=_MAX_GENERATION_TOKENS)
    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.TransportError):
        raise
    except Exception as error:
        request = httpx.Request("POST", "https://profile-analysis-provider.invalid")
        raise httpx.TransportError("profile analysis provider failed", request=request) from error


def _failed_attempt(
    context: ProfileAnalysisAttemptContext,
    failure_code: str,
    retryable_failure: bool,
) -> ProfileAnalysisAttempt:
    completed_at = profile_analysis_clock_utc(context.clock)
    next_retry_at = (
        context.retry_policy.next_retry_at(context.due, completed_at) if retryable_failure else None
    )
    return ProfileAnalysisAttempt(
        analysis_id=context.uuid_factory(),
        person_id=context.person.person_id,
        analysis_type=context.due.analysis_type,
        status=ProfileAnalysisStatus.FAILED,
        content=None,
        input_revision=context.person.input_revision,
        input_fingerprint=context.fingerprint,
        prompt_version=context.prompt_version,
        provider=context.text_service.provider,
        model=context.text_service.default_model,
        started_at=context.started_at,
        completed_at=completed_at,
        failure_code=failure_code,
        retryable=next_retry_at is not None,
        next_retry_at=next_retry_at,
        attempt_number=context.due.attempt_number,
    )


def _http_failure(error: httpx.HTTPStatusError) -> tuple[str, bool]:
    status = error.response.status_code
    if status == 429:
        return "provider_rate_limited", True
    if status >= 500:
        return "provider_unavailable", True
    return "provider_rejected", False


def _prompt_version(analysis_type: ProfileAnalysisType) -> str:
    return (
        SALES_PROFILE_PROMPT_VERSION
        if analysis_type is ProfileAnalysisType.SALES
        else CONTACT_TRACING_PROFILE_PROMPT_VERSION
    )


def _validate_settings(
    batch_size: int,
    claim_lease: timedelta,
    max_attempts: int,
    retry_base: timedelta,
    retry_cap: timedelta,
) -> None:
    if batch_size < 1 or max_attempts < 1:
        raise ValueError("profile analysis bounds must be positive")
    if claim_lease <= timedelta(0) or retry_base <= timedelta(0) or retry_cap < retry_base:
        raise ValueError("profile analysis durations are invalid")
