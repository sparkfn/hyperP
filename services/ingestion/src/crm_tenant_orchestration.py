"""Lead-owned request-building seams for #307 operator and schedule orchestration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
)
from src.standalone_crm_census_types import StandaloneCrmStreamKind, _text


@dataclass(frozen=True)
class ScheduledSourceSyncRequestInput:
    """Already-authoritative inputs for a manually invoked schedule dispatch.

    The schedule task must check its two default-off gates before obtaining this
    input.  Capturing heads is owned by the production authority composition,
    not by the task itself.
    """

    source_instance_id: str
    control_instance_id: str
    occurrence_key: str
    selected_kinds: tuple[StandaloneCrmStreamKind, ...]
    budget: StandaloneCrmBudget
    policy_version: str
    association_contract_version: str
    configuration_digest: str
    authority: SourceSyncAuthority

    def __post_init__(self) -> None:
        for field in (
            "source_instance_id",
            "control_instance_id",
            "occurrence_key",
            "policy_version",
            "association_contract_version",
            "configuration_digest",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        if not isinstance(self.budget, StandaloneCrmBudget):
            raise ValueError("scheduled source sync requires a bounded budget")
        if not isinstance(self.authority, SourceSyncAuthority):
            raise ValueError("scheduled source sync requires captured authority")


def build_scheduled_source_sync_request(
    input_value: ScheduledSourceSyncRequestInput,
) -> SourceSyncCensusRequest:
    """Build the deterministic bounded source-sync request without I/O."""
    return SourceSyncCensusRequest(
        "bitrix_chat",
        input_value.source_instance_id,
        input_value.control_instance_id,
        input_value.occurrence_key,
        input_value.selected_kinds,
        input_value.budget,
        input_value.policy_version,
        input_value.association_contract_version,
        input_value.configuration_digest,
        input_value.authority,
    )


@dataclass(frozen=True)
class CrmTenantOperatorDispatch:
    """A Celery-only operator boundary; commands never invoke a live source inline."""

    task_name: str
    task_id: str


class CrmTenantOperatorCommands:
    """Small dispatch facade for prepare/project/activate/rollback/status/reconcile/source-sync."""

    def __init__(self, dispatch: Callable[[str, object], object]) -> None:
        self._dispatch = dispatch

    def prepare(self, payload: dict[str, object]) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.prepare", payload)

    def project(self, payload: dict[str, object]) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.project", payload)

    def activate(self, payload: dict[str, object]) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.activate", payload)

    def rollback(self, payload: dict[str, object]) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.rollback", payload)

    def reconcile(self, census_id: str) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.reconcile", census_id)

    def source_sync(self, payload: dict[str, object]) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.source_sync", payload)

    def status(self, census_id: str) -> CrmTenantOperatorDispatch:
        return self._enqueue("src.crm_tenant_operator_tasks.status", census_id)

    def _enqueue(self, task_name: str, argument: object) -> CrmTenantOperatorDispatch:
        result = self._dispatch(task_name, argument)
        task_id = getattr(result, "id", None)
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("operator dispatch did not return a task identity")
        return CrmTenantOperatorDispatch(task_name, task_id)
