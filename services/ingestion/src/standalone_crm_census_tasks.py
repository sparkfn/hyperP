"""Default-off Celery controls for the standalone CRM census plane."""

from __future__ import annotations

import json
from collections.abc import Mapping

from celery import Task, current_app, shared_task

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.crm_company_membership import CrmCompanyMembershipRepository
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_source_fact_repository import StandaloneCrmSourceFactRepository
from src.standalone_crm_census_authority import UnavailableStandaloneCrmCensusAuthority
from src.standalone_crm_census_control import StandaloneCrmCensusService
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_models import parse_census_request
from src.standalone_crm_child_contracts import ContactSourceChildEnvelope, LeadSourceChildEnvelope
from src.standalone_crm_company_child import StandaloneCrmCompanySourceHandler
from src.standalone_crm_contact_child import StandaloneCrmContactSourceHandler
from src.standalone_crm_lead_child import StandaloneCrmLeadSourceHandler
from src.standalone_crm_source_child_client import (
    StandaloneCrmSourceChildBitrixSession,
    StandaloneCrmSourceChildBitrixSessionFactory,
)
from src.standalone_crm_source_child_runtime import (
    SOURCE_CHILD_TASK_NAME,
    StandaloneCrmSourceChildClaim,
    StandaloneCrmSourceChildClient,
    StandaloneCrmSourceChildRegistry,
    StandaloneCrmSourceChildRuntime,
)
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactPage,
    StandaloneCrmSourceFactReceipt,
)
from src.standalone_crm_source_fact_writer import StandaloneCrmSourceFactWriter

_SOURCE_CHILD_FENCE_LEASE_SECONDS: int = 120
_LEASE_HELD_RETRY_COUNTDOWN_SECONDS: int = 45
_LEASE_HELD_MAX_RETRIES: int = 3


def _service() -> tuple[StandaloneCrmCensusService, Neo4jClient]:
    client = Neo4jClient(get_settings())
    return (
        StandaloneCrmCensusService(
            StandaloneCrmCensusRepository(client),
            UnavailableStandaloneCrmCensusAuthority(),
            publisher=_CeleryStandaloneCrmChildPublisher(),
        ),
        client,
    )


def _runtime_state(census_id: str, operation: str) -> str | None:
    service, client = _service()
    try:
        if operation == "reconcile":
            return service.reconcile(census_id).state
        if operation == "repair":
            return service.repair(census_id).state
        if operation == "classify":
            return str(service.classify(census_id))
        raise ValueError("unsupported standalone census runtime operation")
    finally:
        client.close()


@shared_task(name="src.standalone_crm_census_tasks.start_standalone_crm_census")
def start_standalone_crm_census(raw_request: Mapping[str, object]) -> str:
    """Validate then fail closed unless an authority adapter is installed by #274/#275."""
    request = parse_census_request(raw_request)
    service, client = _service()
    try:
        return service.start(request).census_id
    finally:
        client.close()


@shared_task(name="src.standalone_crm_census_tasks.reconcile_standalone_crm_census")
def reconcile_standalone_crm_census(census_id: str) -> str | None:
    """Classify durable uncertainty and repair the outbox; never invokes a CRM source."""
    return _runtime_state(census_id, "reconcile")


@shared_task(name="src.standalone_crm_census_tasks.run_standalone_crm_census_parent")
def run_standalone_crm_census_parent(census_id: str) -> str:
    """Run the durable parent state machine; unavailable authority remains fail-closed."""
    service, client = _service()
    try:
        return service.run_parent(census_id).state
    finally:
        client.close()


@shared_task(name="src.standalone_crm_census_tasks.cancel_standalone_crm_census")
def cancel_standalone_crm_census(census_id: str, actor: str, reason: str) -> bool:
    service, client = _service()
    try:
        return service.cancel(census_id, actor, reason)
    finally:
        client.close()


@shared_task(name="src.standalone_crm_census_tasks.resume_standalone_crm_census")
def resume_standalone_crm_census(census_id: str) -> str:
    service, client = _service()
    try:
        return service.resume(census_id).state
    finally:
        client.close()


@shared_task(name="src.standalone_crm_census_tasks.recover_standalone_crm_publication")
def recover_standalone_crm_publication(census_id: str) -> str | None:
    """Repair durable child publications; absent handlers remain fail-closed."""
    return _runtime_state(census_id, "repair")


@shared_task(name="src.standalone_crm_census_tasks.classify_standalone_crm_calls")
def classify_standalone_crm_calls(census_id: str) -> str | None:
    """Classify unclosed reservations without CRM or broker I/O."""
    return _runtime_state(census_id, "classify")


class _CeleryStandaloneCrmChildPublisher:
    """Publish only the exact durable outbox payload and deterministic task identity."""

    def has_handler(self, task_name: str) -> bool:
        return task_name == SOURCE_CHILD_TASK_NAME and task_name in current_app.tasks

    def publish(self, task_name: str, task_id: str, queue: str, payload_json: str) -> None:
        raw = json.loads(payload_json)
        if not isinstance(raw, dict):
            raise RuntimeError("standalone CRM child publication is malformed")
        current_app.send_task(task_name, args=(raw,), task_id=task_id, queue=queue)


class _ContactSourceFacts:
    """One owned bridge: #302 writer plus exact deferred-contact receipt lookup."""

    def __init__(
        self,
        writer: StandaloneCrmSourceFactWriter,
        repository: StandaloneCrmSourceFactRepository,
    ) -> None:
        self._writer = writer
        self._repository = repository

    def write(self, page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactCommitResult:
        return self._writer.write(page)

    def pending_contact_receipt(
        self,
        envelope: ContactSourceChildEnvelope,
        binding_subject_id: int,
    ) -> StandaloneCrmSourceFactReceipt:
        return self._repository.pending_contact_receipt(envelope, binding_subject_id)

    def pending_lead_receipt(
        self,
        envelope: LeadSourceChildEnvelope,
        checkpoint: StandaloneCrmCheckpoint,
    ) -> StandaloneCrmSourceFactReceipt | None:
        return self._repository.pending_lead_receipt(envelope, checkpoint)


class _ContactHandlerAdapter:
    def __init__(self, handler: StandaloneCrmContactSourceHandler) -> None:
        self._handler = handler

    def run(
        self,
        claim: StandaloneCrmSourceChildClaim,
        client: StandaloneCrmSourceChildClient,
    ) -> str:
        if not isinstance(client, StandaloneCrmSourceChildBitrixSession):
            raise RuntimeError("contact handler requires the reserved Bitrix child session")
        return self._handler.run(claim, client)


class _LeadHandlerAdapter:
    def __init__(self, handler: StandaloneCrmLeadSourceHandler) -> None:
        self._handler = handler

    def run(
        self,
        claim: StandaloneCrmSourceChildClaim,
        client: StandaloneCrmSourceChildClient,
    ) -> str:
        if not isinstance(client, StandaloneCrmSourceChildBitrixSession):
            raise RuntimeError("lead handler requires the reserved Bitrix child session")
        return self._handler.run(claim, client)


class _CompanyHandlerAdapter:
    def __init__(self, handler: StandaloneCrmCompanySourceHandler) -> None:
        self._handler = handler

    def run(
        self,
        claim: StandaloneCrmSourceChildClaim,
        client: StandaloneCrmSourceChildClient,
    ) -> str:
        if not isinstance(client, StandaloneCrmSourceChildBitrixSession):
            raise RuntimeError("company handler requires the reserved Bitrix child session")
        return self._handler.run(claim, client)


@shared_task(name=SOURCE_CHILD_TASK_NAME, bind=True)
def run_standalone_crm_census_unit(self: Task, raw_payload: Mapping[str, object]) -> str:
    """Run one parent-published child through the installed fenced runtime."""
    request = getattr(self, "request", None)
    task_id = getattr(request, "id", None)
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("standalone CRM source child requires its Celery task identity")
    runtime, client = _source_child_runtime()
    try:
        result = runtime.run(raw_payload, worker_id=task_id)
        if result in {
            "lease_held_retryable",
            "publication_pending_retryable",
            "convergence_retryable",
        }:
            raise self.retry(
                countdown=_LEASE_HELD_RETRY_COUNTDOWN_SECONDS,
                max_retries=_LEASE_HELD_MAX_RETRIES,
            )
        return result
    finally:
        client.close()


def _source_child_runtime() -> tuple[StandaloneCrmSourceChildRuntime, Neo4jClient]:
    """Construct the closed production child graph without accepting broker-supplied authority."""
    client = Neo4jClient(get_settings())
    census = StandaloneCrmCensusRepository(client)
    source_fact_repository = StandaloneCrmSourceFactRepository(client)
    source_facts = StandaloneCrmSourceFactWriter(source_fact_repository)
    memberships = CrmCompanyMembershipRepository(client)
    registry = StandaloneCrmSourceChildRegistry(
        {
            "contact": _ContactHandlerAdapter(
                StandaloneCrmContactSourceHandler(
                    _ContactSourceFacts(source_facts, source_fact_repository),
                    memberships,
                    census,
                )
            ),
            "lead": _LeadHandlerAdapter(
                StandaloneCrmLeadSourceHandler(
                    _ContactSourceFacts(source_facts, source_fact_repository), memberships
                )
            ),
            "company": _CompanyHandlerAdapter(StandaloneCrmCompanySourceHandler(memberships)),
        }
    )
    return (
        StandaloneCrmSourceChildRuntime(
            census,
            registry,
            StandaloneCrmSourceChildBitrixSessionFactory(get_settings(), census),
        ),
        client,
    )
