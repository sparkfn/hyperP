"""Repository-backed reservation hook for bounded standalone CRM source calls."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

from src.config import Settings
from src.connectors.bitrix_openlines.client import (
    BitrixHttpCallIntent,
    BitrixOpenLinesClient,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_models import (
    StandaloneCrmCallIntent,
    StandaloneCrmCallKind,
    StandaloneCrmCallOutcome,
    StandaloneCrmCensusRequest,
    StandaloneCrmStreamKind,
)


class StandaloneCrmCensusHttpReservationHook:
    """Each physical HTTP retry receives a distinct durable reservation intent."""

    def __init__(
        self,
        repository: StandaloneCrmCensusCallRepository,
        request: StandaloneCrmCensusRequest,
        census_id: str,
        generation: int,
        fence_token: int,
        child_task_id: str | None = None,
        effective_deadline: str | None = None,
    ) -> None:
        self._repository = repository
        self._request = request
        self._census_id = census_id
        self._generation = generation
        self._fence_token = fence_token
        self._child_task_id = child_task_id
        self._effective_deadline = effective_deadline
        self._reserved: dict[str, StandaloneCrmCallIntent] = {}

    def reserve(self, intent: BitrixHttpCallIntent) -> bool:
        metadata = intent.metadata
        if metadata is None:
            return False
        task_id = self._task_id_for(metadata.call_kind)
        if task_id is None:
            return False
        call_intent = StandaloneCrmCallIntent(
            self._census_id,
            self._generation,
            intent.intent_id,
            1,
            _call_kind(metadata.call_kind),
            _stream_kind(metadata.stream_kind),
            intent.retry_ordinal,
            self._request.budget.occurrence_deadline,
            metadata.cursor,
            metadata.subject_id,
            task_id,
            self._effective_deadline,
        )
        sequence = self._repository.reserve_call_with_sequence(
            call_intent, self._fence_token, self._request
        )
        if sequence is not None:
            self._reserved[intent.intent_id] = replace(call_intent, sequence=sequence)
        return sequence is not None

    def _task_id_for(self, call_kind: str) -> str | None:
        if call_kind == "probe":
            return f"standalone-crm-parent:{self._census_id}:{self._generation}"
        return self._child_task_id

    def record_outcome(
        self,
        intent: BitrixHttpCallIntent,
        state: Literal["succeeded", "failed", "unknown"],
        error_code: str | None = None,
    ) -> None:
        call_intent = self._reserved_intent(intent)
        if call_intent.call_kind == "probe" and state == "succeeded":
            raise RuntimeError("probe outcome requires a parsed durable upper bound")
        outcome = StandaloneCrmCallOutcome(
            intent.intent_id,
            call_intent.call_kind,
            state,
            _now_utc(),
            None,
            _required_error_code(state, error_code),
        )
        if not self._repository.record_call_outcome(outcome):
            raise RuntimeError("durable Bitrix call outcome was rejected")

    def record_probe_upper_bound(self, intent: BitrixHttpCallIntent, upper_id: int) -> None:
        call_intent = self._reserved_intent(intent)
        outcome = StandaloneCrmCallOutcome(
            intent.intent_id,
            call_intent.call_kind,
            "succeeded",
            _now_utc(),
            upper_id,
        )
        if not self._repository.record_call_outcome(outcome):
            raise RuntimeError("durable Bitrix probe outcome was rejected")

    def _reserved_intent(self, intent: BitrixHttpCallIntent) -> StandaloneCrmCallIntent:
        call_intent = self._reserved.get(intent.intent_id)
        if call_intent is None:
            raise RuntimeError("Bitrix call outcome has no local durable reservation")
        return call_intent


class StandaloneCrmCensusCallRepository(Protocol):
    """The narrow durable surface needed around one physical source request."""

    def reserve_call_with_sequence(
        self,
        intent: StandaloneCrmCallIntent,
        fence_token: int,
        budget: StandaloneCrmCensusRequest,
    ) -> int | None: ...

    def record_call_outcome(self, outcome: StandaloneCrmCallOutcome) -> bool: ...


@dataclass(frozen=True)
class StandaloneCrmCensusBitrixProbe:
    """Read-only probe facade that owns one bounded, reservation-backed client."""

    client: BitrixOpenLinesClient

    def upper_bound(self, stream_kind: StandaloneCrmStreamKind) -> int:
        if stream_kind == "contact":
            return self.client.probe_crm_contact_upper_id()
        if stream_kind == "lead":
            return self.client.probe_crm_lead_upper_id()
        return self.client.probe_crm_company_upper_id()

    def close(self) -> None:
        self.client.close()


class StandaloneCrmCensusBitrixProbeFactory:
    """Constructs a client only after the parent runtime has passed fail-closed gates."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def create(
        self,
        repository: StandaloneCrmCensusRepository,
        request: StandaloneCrmCensusRequest,
        census_id: str,
        generation: int,
        fence_token: int,
        child_task_id: str | None = None,
    ) -> StandaloneCrmCensusBitrixProbe:
        effective_deadline = _effective_deadline(
            request.budget.occurrence_deadline, request.budget.max_runtime_seconds_per_attempt
        )
        deadline = _deadline_monotonic(effective_deadline)
        hook = StandaloneCrmCensusHttpReservationHook(
            repository,
            request,
            census_id,
            generation,
            fence_token,
            child_task_id,
            effective_deadline,
        )
        return StandaloneCrmCensusBitrixProbe(
            BitrixOpenLinesClient(
                base_url=self._settings.bitrix_openlines_api_base_url.get_secret_value(),
                timeout_seconds=self._settings.bitrix_openlines_api_timeout_seconds,
                max_attempts=self._settings.bitrix_openlines_api_max_attempts,
                request_delay_seconds=self._settings.bitrix_openlines_api_request_delay_seconds,
                max_request_count=request.budget.max_calls_per_attempt,
                deadline_monotonic=deadline,
                reservation_hook=hook,
            )
        )


def _call_kind(value: str) -> StandaloneCrmCallKind:
    if value == "probe":
        return "probe"
    if value == "page":
        return "page"
    if value == "company_binding":
        return "company_binding"
    raise ValueError("unsupported standalone CRM HTTP call kind")


def _stream_kind(value: str | None) -> StandaloneCrmStreamKind | None:
    if value is None:
        return None
    if value == "contact":
        return "contact"
    if value == "lead":
        return "lead"
    if value == "company":
        return "company"
    raise ValueError("unsupported standalone CRM HTTP stream kind")


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def _required_error_code(
    state: Literal["succeeded", "failed", "unknown"], error_code: str | None
) -> str | None:
    if state == "succeeded":
        return None
    return error_code if error_code is not None else "unspecified_failure"


def _deadline_monotonic(deadline: str) -> float:
    parsed = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("standalone census deadline must be timezone-aware")
    remaining = (parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise ValueError("standalone census deadline is already exhausted")
    return time.monotonic() + remaining


def _effective_deadline(occurrence_deadline: str, attempt_runtime_seconds: int) -> str:
    occurrence = datetime.fromisoformat(occurrence_deadline.replace("Z", "+00:00")).astimezone(UTC)
    attempt = datetime.now(UTC) + timedelta(seconds=attempt_runtime_seconds)
    return min(occurrence, attempt).isoformat().replace("+00:00", "Z")
