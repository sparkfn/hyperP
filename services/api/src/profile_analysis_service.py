"""Direct execution service for one on-demand Person profile analysis request."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from src.config import AppConfig
from src.proclaude.service import ProclaudeService
from src.profile_analysis_client import Neo4jClient
from src.profile_analysis_repository import (
    ClaimedProfileAnalysisPerson,
    Neo4jProfileAnalysisRepository,
)
from src.profile_analysis_worker import run_profile_analysis_person
from src.profile_analysis_worker_types import (
    LlmProfileAnalysisTextService,
    ProfileAnalysisExecutionSummary,
)

logger = logging.getLogger(__name__)


def run_profile_analysis_request(
    request_id: str,
    app_config: AppConfig,
) -> ProfileAnalysisExecutionSummary:
    """Claim, generate, persist, and finalize one request in the API process.

    This function is synchronous by design and must be called through
    :func:`asyncio.to_thread` by the FastAPI route. The Person lease remains the
    cross-request concurrency guard; no Celery delivery or Redis broker is used.
    """
    client = Neo4jClient(app_config)
    repository = Neo4jProfileAnalysisRepository(client)
    claim_token = uuid4().hex
    claimed = False
    claim_released = False
    claimed_person_id: str | None = None
    try:
        person = _claim_request(repository, request_id, claim_token, app_config)
        if person is None:
            return _empty_summary()
        claimed = True
        claimed_person_id = person.person_id
        summary = run_profile_analysis_person(
            repository=repository,
            text_service=LlmProfileAnalysisTextService(ProclaudeService()),
            person=person,
            claim_token=claim_token,
            claim_lease=timedelta(seconds=app_config.profile_analysis_claim_lease_seconds),
            clock=lambda: datetime.now(UTC),
            release_claim=False,
        )
        if summary["attempted"] != 1 or summary["unexpected_failures"] > 0:
            raise RuntimeError("direct profile analysis did not complete one safe attempt")
        repository.complete_request(
            request_id=request_id,
            claim_token=claim_token,
            status=_terminal_status(summary),
        )
        if repository.release_claim(person_id=claimed_person_id, claim_token=claim_token):
            summary["released"] += 1
        claim_released = True
        return summary
    except Exception:
        if claimed:
            try:
                repository.complete_request(
                    request_id=request_id,
                    claim_token=claim_token,
                    status="failed",
                )
            except Exception:
                logger.exception(
                    "Failed to finalize direct profile-analysis request %s",
                    request_id,
                )
        raise
    finally:
        if claimed and claimed_person_id is not None and not claim_released:
            try:
                repository.release_claim(person_id=claimed_person_id, claim_token=claim_token)
            except Exception:
                logger.exception(
                    "Failed to release direct profile-analysis claim for request %s",
                    request_id,
                )
        client.close()


def _empty_summary() -> ProfileAnalysisExecutionSummary:
    return {
        "claimed": 0,
        "attempted": 0,
        "succeeded": 0,
        "failed": 0,
        "obsolete": 0,
        "unexpected_failures": 0,
        "released": 0,
        "has_more": False,
    }


def _claim_request(
    repository: Neo4jProfileAnalysisRepository,
    request_id: str,
    claim_token: str,
    app_config: AppConfig,
) -> ClaimedProfileAnalysisPerson | None:
    """Wait for a concurrent type's Person lease instead of leaving work queued.

    The existing graph model serializes both analysis types with one Person
    lease. When two cards request work together, the second API thread waits
    locally for the first direct invocation to release that lease; it never
    hands the request to Celery.
    """
    while True:
        now = datetime.now(UTC)
        person = repository.claim_request(
            request_id=request_id,
            claim_token=claim_token,
            now=now,
            claim_until=now + timedelta(seconds=app_config.profile_analysis_claim_lease_seconds),
        )
        if person is not None:
            return person
        if not repository.request_is_waiting(request_id=request_id):
            repository.obsolete_inactive_request(request_id=request_id)
            return None
        time.sleep(0.25)


def _terminal_status(summary: ProfileAnalysisExecutionSummary) -> str:
    if summary["succeeded"] > 0:
        return "succeeded"
    if summary["obsolete"] > 0:
        return "obsolete"
    return "failed"
