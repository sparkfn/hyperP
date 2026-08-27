"""Celery internal-control entries for #273; explicitly registered and never Beat scheduled."""

from __future__ import annotations

from collections.abc import Callable

from celery import Task

from src.celery_app import celery_app
from src.standalone_crm_census_control import StandaloneCrmCensusControl
from src.standalone_crm_census_requests import operator_request_from_json


class StandaloneCrmCensusTaskUnavailableError(RuntimeError):
    """Raised before graph, client, broker-child, or source I/O when no control is installed."""


_control_factory: Callable[[], StandaloneCrmCensusControl] | None = None


def register_standalone_crm_census_control(
    factory: Callable[[], StandaloneCrmCensusControl] | None,
) -> None:
    """Install an internal runtime factory; production remains default-off when absent."""
    global _control_factory
    _control_factory = factory


def _control() -> StandaloneCrmCensusControl:
    if _control_factory is None:
        raise StandaloneCrmCensusTaskUnavailableError(
            "standalone CRM census control is unavailable until #275 authority "
            "and child handlers exist"
        )
    return _control_factory()


def _required_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty")
    return value


@celery_app.task(bind=True, name="src.standalone_crm_census_tasks.start_parent_census")  # type: ignore[untyped-decorator]
def start_parent_census_task(self: Task, request_json: str) -> dict[str, object]:
    """Start from an immutable operator request supplied through the internal queue only."""
    request = operator_request_from_json(request_json)
    result = _control().start(request, task_id=self.request.id or request.occurrence_key)
    return {"census_id": result.census_id, "generation": result.generation, "state": result.state}


@celery_app.task(name="src.standalone_crm_census_tasks.status_parent_census")  # type: ignore[untyped-decorator]
def status_parent_census_task(census_id: str) -> dict[str, object]:
    """Return internal durable status; this is not an API/MCP endpoint."""
    status = _control().status(_required_id(census_id, "census_id"))
    return {} if status is None else status


@celery_app.task(bind=True, name="src.standalone_crm_census_tasks.run_parent_census")  # type: ignore[untyped-decorator]
def run_parent_census_task(self: Task, census_id: str) -> dict[str, object]:
    """Resume an admitted parent delivery through the registered internal control plane."""
    result = _control().resume(
        _required_id(census_id, "census_id"), task_id=self.request.id or census_id
    )
    return {"census_id": result.census_id, "generation": result.generation, "state": result.state}


@celery_app.task(name="src.standalone_crm_census_tasks.cancel_parent_census")  # type: ignore[untyped-decorator]
def cancel_parent_census_task(census_id: str, actor: str, reason: str) -> dict[str, object]:
    """Record durable cancellation only; no source or broker I/O occurs here."""
    count = _control().cancel(_required_id(census_id, "census_id"), actor=actor, reason=reason)
    return {"census_id": census_id, "cancelled_children": count}


@celery_app.task(name="src.standalone_crm_census_tasks.reconcile_parent_census")  # type: ignore[untyped-decorator]
def reconcile_parent_census_task(census_id: str) -> dict[str, object]:
    """Derive terminal state/totals solely from durable census state."""
    state, expected_units = _control().reconcile(_required_id(census_id, "census_id"))
    return {"census_id": census_id, "state": state, "expected_units": expected_units}


@celery_app.task(name="src.standalone_crm_census_tasks.repair_child_publication")  # type: ignore[untyped-decorator]
def repair_child_publication_task(publication_id: str) -> dict[str, str]:
    """Delegate outbox repair to an installed observer; default-off does no broker I/O."""
    checked = _required_id(publication_id, "publication_id")
    _control().repair(checked)
    return {"publication_id": checked, "status": "repaired"}


@celery_app.task(name="src.standalone_crm_census_tasks.classify_reserved_call_unknown")  # type: ignore[untyped-decorator]
def classify_reserved_call_unknown_task(census_id: str, intent_id: str) -> dict[str, object]:
    """Consume one unresolved current reservation through the internal recovery plane."""
    checked_census_id = _required_id(census_id, "census_id")
    checked_intent_id = _required_id(intent_id, "intent_id")
    changed = _control().classify_reserved_call_unknown(
        checked_census_id, intent_id=checked_intent_id
    )
    return {"census_id": checked_census_id, "intent_id": checked_intent_id, "classified": changed}
