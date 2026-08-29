"""Production wiring tests for the standalone CRM source-child Celery task."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from src import standalone_crm_census_tasks as task_module
from src.standalone_crm_company_child import StandaloneCrmCompanySourceHandler
from src.standalone_crm_contact_child import StandaloneCrmContactSourceHandler
from src.standalone_crm_lead_child import StandaloneCrmLeadSourceHandler
from src.standalone_crm_source_child_runtime import SOURCE_CHILD_TASK_NAME


@dataclass
class _GraphClient:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _Runtime:
    calls: list[tuple[object, str]] = field(default_factory=list)
    result: str = "unit_completed"

    def run(self, raw_payload: object, *, worker_id: str) -> str:
        self.calls.append((raw_payload, worker_id))
        return self.result


@dataclass
class _BoundTask:
    request: object

    def retry(self, *, countdown: int, max_retries: int) -> str:
        raise AssertionError("retry was not expected")


@dataclass
class _RetryingBoundTask:
    request: object
    retries: list[tuple[int, int]] = field(default_factory=list)

    def retry(self, *, countdown: int, max_retries: int) -> None:
        self.retries.append((countdown, max_retries))
        raise _RetryRequestedError()


class _RetryRequestedError(Exception):
    pass


def test_source_child_task_uses_celery_task_identity_and_closes_its_graph_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime()
    graph = _GraphClient()
    raw = {"publication": "exact"}
    monkeypatch.setattr(task_module, "_source_child_runtime", lambda: (runtime, graph))

    result = task_module.run_standalone_crm_census_unit.run.__func__(
        _BoundTask(SimpleNamespace(id="published-task")), raw
    )

    assert result == "unit_completed"
    assert runtime.calls == [(raw, "published-task")]
    assert graph.closed is True


def test_source_child_task_rejects_an_absent_celery_task_id_before_runtime_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        task_module,
        "_source_child_runtime",
        lambda: pytest.fail("raw child must fail before runtime construction"),
    )

    with pytest.raises(RuntimeError, match="Celery task identity"):
        task_module.run_standalone_crm_census_unit.run.__func__(
            _BoundTask(SimpleNamespace(id=None)), {}
        )


def test_source_child_task_retries_only_an_exact_active_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(result="lease_held_retryable")
    graph = _GraphClient()
    task = _RetryingBoundTask(SimpleNamespace(id="published-task"))
    monkeypatch.setattr(task_module, "_source_child_runtime", lambda: (runtime, graph))

    with pytest.raises(_RetryRequestedError):
        task_module.run_standalone_crm_census_unit.run.__func__(task, {"publication": "exact"})

    assert task.retries == [
        (
            task_module._LEASE_HELD_RETRY_COUNTDOWN_SECONDS,  # noqa: SLF001
            task_module._LEASE_HELD_MAX_RETRIES,  # noqa: SLF001
        )
    ]
    assert (
        task_module._LEASE_HELD_RETRY_COUNTDOWN_SECONDS  # noqa: SLF001
        * task_module._LEASE_HELD_MAX_RETRIES  # noqa: SLF001
        > task_module._SOURCE_CHILD_FENCE_LEASE_SECONDS  # noqa: SLF001
    )
    assert graph.closed is True


def test_source_child_task_does_not_retry_terminal_claim_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _Runtime(result="terminal_denied")
    graph = _GraphClient()
    task = _RetryingBoundTask(SimpleNamespace(id="published-task"))
    monkeypatch.setattr(task_module, "_source_child_runtime", lambda: (runtime, graph))

    assert task_module.run_standalone_crm_census_unit.run.__func__(task, {}) == "terminal_denied"
    assert task.retries == []
    assert graph.closed is True


def test_production_runtime_wires_the_closed_contact_lead_company_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _GraphClient()
    monkeypatch.setattr(task_module, "Neo4jClient", lambda _: graph)
    monkeypatch.setattr(task_module, "get_settings", lambda: object())

    runtime, returned_client = task_module._source_child_runtime()

    registry = runtime._registry  # noqa: SLF001 - verifies the private closed production wiring
    assert returned_client is graph
    assert registry.has_task_handler(SOURCE_CHILD_TASK_NAME) is True
    contact = registry.handler_for("contact")
    lead = registry.handler_for("lead")
    company = registry.handler_for("company")
    assert isinstance(contact, task_module._ContactHandlerAdapter)  # noqa: SLF001
    assert isinstance(lead, task_module._LeadHandlerAdapter)  # noqa: SLF001
    assert isinstance(company, task_module._CompanyHandlerAdapter)  # noqa: SLF001
    assert isinstance(
        contact._handler,  # noqa: SLF001 - adapter boundary is intentional
        StandaloneCrmContactSourceHandler,
    )
    assert isinstance(
        lead._handler,  # noqa: SLF001 - adapter boundary is intentional
        StandaloneCrmLeadSourceHandler,
    )
    assert isinstance(
        company._handler,  # noqa: SLF001 - adapter boundary is intentional
        StandaloneCrmCompanySourceHandler,
    )
