"""Closed, fenced runtime for one parent-issued standalone CRM source unit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_models import StandaloneCrmChildEnvelope
from src.standalone_crm_census_types import StandaloneCrmStreamKind
from src.standalone_crm_source_child_authority import (
    SOURCE_CHILD_TASK_NAME,
    StandaloneCrmSourceChildClaim,
    build_claim,
    parse_publication_payload,
)

__all__ = [
    "SOURCE_CHILD_TASK_NAME",
    "StandaloneCrmSourceChildClaim",
    "StandaloneCrmSourceChildClaimOutcome",
    "StandaloneCrmSourceChildClient",
    "StandaloneCrmSourceChildClientFactory",
    "StandaloneCrmSourceChildHandler",
    "StandaloneCrmSourceChildRegistry",
    "StandaloneCrmSourceChildRuntime",
    "StandaloneCrmSourceChildRuntimeOutcome",
]

type StandaloneCrmSourceChildClaimDecision = Literal[
    "claimed", "lease_held_retryable", "publication_pending_retryable", "terminal_denied"
]
type StandaloneCrmSourceChildRuntimeOutcome = Literal[
    "lease_held_retryable",
    "publication_pending_retryable",
    "terminal_denied",
    "unit_completed",
    "unit_no_work",
    "paused_with_checkpoint",
    "occurrence_exhausted",
    "convergence_retryable",
]


@dataclass(frozen=True)
class StandaloneCrmSourceChildClaimOutcome:
    """Exact durable admission result before a source client can exist."""

    decision: StandaloneCrmSourceChildClaimDecision
    claim: StandaloneCrmSourceChildClaim | None = None


class StandaloneCrmSourceChildClient(Protocol):
    """A source client is created only after the durable child claim succeeds."""

    def close(self) -> None: ...


class StandaloneCrmSourceChildHandler(Protocol):
    """A registered handler owns exactly one bounded source stream."""

    def run(
        self, claim: StandaloneCrmSourceChildClaim, client: StandaloneCrmSourceChildClient
    ) -> str: ...


class StandaloneCrmSourceChildClientFactory(Protocol):
    def create(self, claim: StandaloneCrmSourceChildClaim) -> StandaloneCrmSourceChildClient: ...


class StandaloneCrmSourceChildRegistry:
    """Closed production registry; generic Celery registration is insufficient."""

    def __init__(
        self, handlers: Mapping[StandaloneCrmStreamKind, StandaloneCrmSourceChildHandler]
    ) -> None:
        if set(handlers) != {"contact", "lead", "company"}:
            raise ValueError(
                "source child registry must contain exactly contact, lead, and company"
            )
        self._handlers = dict(handlers)

    def has_task_handler(self, task_name: str) -> bool:
        return task_name == SOURCE_CHILD_TASK_NAME

    def handler_for(self, stream_kind: StandaloneCrmStreamKind) -> StandaloneCrmSourceChildHandler:
        return self._handlers[stream_kind]


class StandaloneCrmSourceChildRuntime:
    """Claim one exact publication, then consume its bounded source unit safely."""

    def __init__(
        self,
        repository: StandaloneCrmCensusRepository,
        registry: StandaloneCrmSourceChildRegistry,
        client_factory: StandaloneCrmSourceChildClientFactory,
    ) -> None:
        self._repository = repository
        self._registry = registry
        self._client_factory = client_factory

    def run(
        self, raw_payload: Mapping[str, object], *, worker_id: str
    ) -> StandaloneCrmSourceChildRuntimeOutcome:
        payload_json, published = parse_publication_payload(raw_payload)
        if published.task_id != worker_id:
            raise RuntimeError(
                "standalone CRM source child task identity does not match publication"
            )
        if not self._registry.has_task_handler(published.task_name):
            raise RuntimeError("standalone CRM source child handler is not registered")
        outcome = self._claim(published, payload_json, worker_id)
        if outcome.decision != "claimed":
            return outcome.decision
        if outcome.claim is None:
            raise RuntimeError("claimed standalone CRM child has no durable claim")
        claim = outcome.claim
        client = self._client_factory.create(claim)
        try:
            return self._run_claimed_unit(published, payload_json, claim, client, worker_id)
        finally:
            client.close()

    def _claim(
        self,
        published: StandaloneCrmChildEnvelope,
        payload_json: str,
        worker_id: str,
    ) -> StandaloneCrmSourceChildClaimOutcome:
        claimed = self._repository.claim_published_child(
            published,
            owner_id=worker_id,
            payload_json=payload_json,
        )
        if claimed is None:
            if self._repository.published_child_lease_held(
                published, owner_id=worker_id, payload_json=payload_json
            ):
                return StandaloneCrmSourceChildClaimOutcome("lease_held_retryable")
            if self._repository.published_child_preconfirm_pending(
                published, payload_json=payload_json
            ):
                return StandaloneCrmSourceChildClaimOutcome("publication_pending_retryable")
            return StandaloneCrmSourceChildClaimOutcome("terminal_denied")
        return StandaloneCrmSourceChildClaimOutcome("claimed", build_claim(published, claimed))

    def _run_claimed_unit(
        self,
        published: StandaloneCrmChildEnvelope,
        payload_json: str,
        claim: StandaloneCrmSourceChildClaim,
        client: StandaloneCrmSourceChildClient,
        worker_id: str,
    ) -> StandaloneCrmSourceChildRuntimeOutcome:
        handler = self._registry.handler_for(published.stream_kind)
        current = claim
        while True:
            try:
                result = handler.run(current, client)
            except (RuntimeError, ValueError):
                return self._pause(current, "source_effect_failed")
            if result in _COMPLETED_ROW_RESULTS:
                refreshed = self._refresh_after_progress(
                    published, payload_json, current, worker_id
                )
                if refreshed is None:
                    return "convergence_retryable"
                current = refreshed
                continue
            if result in _NO_ROW_RESULTS:
                return self._settle_no_row(current)
            if result == "attempt_exhausted":
                return self._pause(current, "attempt_budget_exhausted")
            if result == "occurrence_exhausted":
                if self._repository.converge_occurrence_exhaustion(
                    current.envelope.unit.census_id,
                    current.envelope.unit.generation,
                ):
                    return "occurrence_exhausted"
                return "convergence_retryable"
            if result == "authority_rejected":
                return "terminal_denied"
            return "convergence_retryable"

    def _refresh_after_progress(
        self,
        published: StandaloneCrmChildEnvelope,
        payload_json: str,
        claim: StandaloneCrmSourceChildClaim,
        worker_id: str,
    ) -> StandaloneCrmSourceChildClaim | None:
        envelope = claim.envelope
        if not self._repository.renew_unit_fence(
            envelope.unit.census_id,
            envelope.unit.generation,
            envelope.unit.stream_kind,
            envelope.unit.fence_token,
            envelope.unit.fence_owner_id,
        ):
            return None
        refreshed = self._repository.refresh_published_child(
            published,
            owner_id=worker_id,
            fence_token=envelope.unit.fence_token,
            payload_json=payload_json,
        )
        return None if refreshed is None else build_claim(published, refreshed)

    def _settle_no_row(
        self, claim: StandaloneCrmSourceChildClaim
    ) -> StandaloneCrmSourceChildRuntimeOutcome:
        checkpoint = claim.checkpoint
        no_work = checkpoint.processed_rows == 0 and checkpoint.skipped_rows == 0
        state = "no_work" if no_work else "completed"
        if not self._repository.settle_unit(
            claim.envelope.unit.census_id,
            claim.envelope.unit.generation,
            claim.envelope.unit.stream_kind,
            claim.envelope.unit.fence_token,
            state,
            no_work=no_work,
        ):
            return "convergence_retryable"
        return "unit_no_work" if no_work else "unit_completed"

    def _pause(
        self, claim: StandaloneCrmSourceChildClaim, detail: str
    ) -> StandaloneCrmSourceChildRuntimeOutcome:
        envelope = claim.envelope
        if self._repository.pause_claimed_unit(
            envelope.unit.census_id,
            envelope.unit.generation,
            envelope.unit.stream_kind,
            envelope.unit.fence_token,
            envelope.unit.fence_owner_id,
            envelope.unit.task_name,
            envelope.unit.task_id,
            envelope.unit.payload_digest,
            envelope.frozen_upper_id,
            claim.checkpoint,
            detail,
            "source child stopped at a durable checkpoint",
        ):
            return "paused_with_checkpoint"
        return "convergence_retryable"


_COMPLETED_ROW_RESULTS = frozenset({"contact_completed", "lead_completed", "company_completed"})
_NO_ROW_RESULTS = frozenset({"no_contact_row", "no_lead_row", "no_company_row"})
