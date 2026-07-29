"""Synchronous execution for one claimed Person profile analysis request."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import httpx

from src.llm.service import ChatMessage
from src.profile_analysis_mapping import ProfileAnalysisTemporalMappingError
from src.profile_analysis_models import (
    ProfileAnalysisAttempt,
    ProfileAnalysisStatus,
    ProfileAnalysisType,
)
from src.profile_analysis_output import (
    ProfileAnalysisOutputError,
    ProfileAnalysisOutputReason,
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
    KnownSensitiveValue,
    ProfileAnalysisPrivacyError,
    snapshot_fingerprint,
)
from src.profile_analysis_worker_types import (
    ProfileAnalysisAttemptContext,
    ProfileAnalysisExecutionCounts,
    ProfileAnalysisExecutionSummary,
    ProfileAnalysisTextService,
    profile_analysis_clock_utc,
)

_MAX_GENERATION_TOKENS = 700
_INVALID_SNAPSHOT_FINGERPRINT = (
    "sha256:" + hashlib.sha256(b"profile-analysis-invalid-snapshot-v1").hexdigest()
)
logger = logging.getLogger(__name__)

_REPAIRABLE_OUTPUT_REASONS: frozenset[ProfileAnalysisOutputReason] = frozenset(
    {
        ProfileAnalysisOutputReason.NOT_TRIMMED,
        ProfileAnalysisOutputReason.TOO_LARGE,
        ProfileAnalysisOutputReason.NOT_PLAIN_TEXT,
        ProfileAnalysisOutputReason.MISSING_LIMITATIONS,
        ProfileAnalysisOutputReason.UNKNOWN_EVIDENCE,
    }
)


def run_profile_analysis_person(
    *,
    repository: ProfileAnalysisRepository,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    claim_token: str,
    claim_lease: timedelta,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID] = uuid4,
    release_claim: bool = True,
) -> ProfileAnalysisExecutionSummary:
    """Process one already claimed person/type without scanning other Persons."""
    _validate_settings(claim_lease)
    counts = ProfileAnalysisExecutionCounts()
    try:
        _process_person(
            repository=repository,
            text_service=text_service,
            person=person,
            clock=clock,
            uuid_factory=uuid_factory,
            counts=counts,
            claim_token=claim_token,
            claim_lease=claim_lease,
        )
    except Exception:
        counts.unexpected_failures += 1
    finally:
        if release_claim:
            try:
                if repository.release_claim(person_id=person.person_id, claim_token=claim_token):
                    counts.released += 1
            except Exception:
                counts.unexpected_failures += 1
    return _summary(False, 1, counts)


def _summary(
    has_more: bool,
    claimed: int,
    counts: ProfileAnalysisExecutionCounts,
) -> ProfileAnalysisExecutionSummary:
    return ProfileAnalysisExecutionSummary(
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
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
    counts: ProfileAnalysisExecutionCounts,
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
    except ProfileAnalysisMappingError as error:
        failure_code = (
            "invalid_snapshot_temporal"
            if isinstance(error, ProfileAnalysisTemporalMappingError)
            else "invalid_snapshot"
        )
        _persist_invalid_snapshot_attempts(
            repository=repository,
            text_service=text_service,
            person=person,
            clock=clock,
            uuid_factory=uuid_factory,
            counts=counts,
            claim_token=claim_token,
            claim_lease=claim_lease,
            failure_code=failure_code,
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
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
) -> ProfileAnalysisAttempt:
    context = _attempt_context(
        text_service=text_service,
        person=person,
        due=due,
        fingerprint=fingerprint,
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
        return _failed_attempt(context, "privacy_snapshot")
    except ProfileAnalysisPrivacyOutputError as error:
        _log_output_validation_failure(person, due, error)
        return _failed_attempt(context, "privacy_output")
    except ProfileAnalysisOutputError as error:
        _log_output_validation_failure(person, due, error)
        return _repair_or_fail_output(
            context=context,
            text_service=text_service,
            person=person,
            due=due,
            messages=messages,
            evidence=evidence,
            known_sensitive_values=bundle.known_sensitive_values,
            error=error,
        )
    except httpx.HTTPStatusError as error:
        return _failed_attempt(context, _http_failure(error))
    except (httpx.TimeoutException, httpx.TransportError):
        return _failed_attempt(context, "provider_unavailable")
    return _succeeded_attempt(context, content)


def _repair_or_fail_output(
    *,
    context: ProfileAnalysisAttemptContext,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    due: DueProfileAnalysis,
    messages: list[ChatMessage],
    evidence: frozenset[str],
    known_sensitive_values: tuple[KnownSensitiveValue, ...],
    error: ProfileAnalysisOutputError,
) -> ProfileAnalysisAttempt:
    if error.reason not in _REPAIRABLE_OUTPUT_REASONS:
        return _failed_attempt(context, "invalid_output")
    try:
        output = _call_provider(text_service, _repair_messages(messages, error.reason))
        content = validate_profile_analysis_output(output, evidence, known_sensitive_values)
    except ProfileAnalysisPrivacyOutputError as repair_error:
        _log_output_validation_failure(person, due, repair_error)
        return _failed_attempt(context, "privacy_output")
    except ProfileAnalysisOutputError as repair_error:
        _log_output_validation_failure(person, due, repair_error)
        return _failed_attempt(context, "invalid_output")
    except httpx.HTTPStatusError as repair_error:
        return _failed_attempt(context, _http_failure(repair_error))
    except (httpx.TimeoutException, httpx.TransportError):
        return _failed_attempt(context, "provider_unavailable")
    return _succeeded_attempt(context, content)


def _repair_messages(
    messages: list[ChatMessage],
    reason: ProfileAnalysisOutputReason,
) -> list[ChatMessage]:
    """Add a safe corrective instruction without returning unsafe model output."""
    if not messages or messages[0].role != "system":
        raise ValueError("profile analysis messages must start with a system prompt")
    correction = (
        "\n\nThe previous response failed the output contract for safe reason "
        f"'{reason.value}'. Regenerate the complete response. Do not mention this correction. "
        "Follow every formatting, evidence, privacy, and Limitations requirement exactly."
    )
    return [ChatMessage(role="system", content=messages[0].content + correction), *messages[1:]]


def _log_output_validation_failure(
    person: ClaimedProfileAnalysisPerson,
    due: DueProfileAnalysis,
    error: ProfileAnalysisOutputError,
) -> None:
    logger.warning(
        "Profile analysis output validation failed person_id=%s analysis_type=%s reason=%s",
        person.person_id,
        due.analysis_type.value,
        error.reason.value,
    )


def _persist_invalid_snapshot_attempts(
    *,
    repository: ProfileAnalysisRepository,
    text_service: ProfileAnalysisTextService,
    person: ClaimedProfileAnalysisPerson,
    clock: Callable[[], datetime],
    uuid_factory: Callable[[], UUID],
    counts: ProfileAnalysisExecutionCounts,
    claim_token: str,
    claim_lease: timedelta,
    failure_code: str,
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
            clock=clock,
            uuid_factory=uuid_factory,
        )
        try:
            counts.record(
                repository.persist_attempt(
                    _failed_attempt(context, failure_code),
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
    counts: ProfileAnalysisExecutionCounts,
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
) -> ProfileAnalysisAttempt:
    completed_at = profile_analysis_clock_utc(context.clock)
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
        retryable=False,
        next_retry_at=None,
        attempt_number=context.due.attempt_number,
    )


def _http_failure(error: httpx.HTTPStatusError) -> str:
    status = error.response.status_code
    if status == 429:
        return "provider_rate_limited"
    if status >= 500:
        return "provider_unavailable"
    return "provider_rejected"


def _prompt_version(analysis_type: ProfileAnalysisType) -> str:
    return (
        SALES_PROFILE_PROMPT_VERSION
        if analysis_type is ProfileAnalysisType.SALES
        else CONTACT_TRACING_PROFILE_PROMPT_VERSION
    )


def _validate_settings(claim_lease: timedelta) -> None:
    if claim_lease <= timedelta(0):
        raise ValueError("profile analysis claim lease must be positive")
