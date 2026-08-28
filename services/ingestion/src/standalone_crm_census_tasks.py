"""Default-off Celery controls for the standalone CRM census plane."""

from __future__ import annotations

from collections.abc import Mapping

from celery import shared_task

from src.config import get_settings
from src.graph.client import Neo4jClient
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_authority import UnavailableStandaloneCrmCensusAuthority
from src.standalone_crm_census_control import StandaloneCrmCensusService
from src.standalone_crm_census_models import parse_census_request


def _service() -> tuple[StandaloneCrmCensusService, Neo4jClient]:
    client = Neo4jClient(get_settings())
    return (
        StandaloneCrmCensusService(
            StandaloneCrmCensusRepository(client),
            UnavailableStandaloneCrmCensusAuthority(),
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
