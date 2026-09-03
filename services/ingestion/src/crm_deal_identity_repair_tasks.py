"""Strict manually invoked Celery adapter for #313; no schedule or chaining."""

from __future__ import annotations

from typing import NotRequired, TypedDict, cast

from src.celery_app import celery_app
from src.crm_deal_identity_repair.cli import parse_arguments


class RepairTaskPayload(TypedDict):
    operation: str
    repair_id: str
    run_id: str
    owner_id: str
    expected_revision: int
    approval_id: str
    unit_id: NotRequired[str]
    authorization_reference: NotRequired[str]
    predecessor_transition_id: NotRequired[str]


_ALLOWED = frozenset(
    {"apply", "verify", "rollback-status", "rollback", "accept", "release-dispatch"}
)
_REQUIRED = frozenset(
    {"operation", "repair_id", "run_id", "owner_id", "expected_revision", "approval_id"}
)
_OPTIONAL = frozenset({"unit_id", "authorization_reference", "predecessor_transition_id"})
_STRING_FIELDS = (_REQUIRED - {"expected_revision"}) | _OPTIONAL


@celery_app.task(name="src.crm_deal_identity_repair_tasks.run_crm_deal_identity_repair_operation")  # type: ignore[untyped-decorator]
def run_crm_deal_identity_repair_operation(payload: RepairTaskPayload) -> dict[str, str]:
    """Run one operator-selected command and return its actual non-secret receipt."""
    from src.crm_deal_identity_repair.integration_runtime import execute_integration

    validated = _validate_payload(payload)
    result: object = execute_integration(parse_arguments(_arguments(validated)))
    if not isinstance(result, dict):
        raise RuntimeError("repair task runtime result is malformed")
    output: dict[str, str] = {}
    for key, value in result.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError("repair task runtime result is malformed")
        output[key] = value
    return output


def _validate_payload(payload: object) -> RepairTaskPayload:
    if (
        not isinstance(payload, dict)
        or set(payload) - (_REQUIRED | _OPTIONAL)
        or not _REQUIRED <= set(payload)
    ):
        raise ValueError("repair task payload schema is invalid")
    for key in _STRING_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("repair task payload string is invalid: " + key)
    operation = payload["operation"]
    if operation not in _ALLOWED:
        raise ValueError("repair task operation is invalid")
    revision = payload["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("repair task revision is invalid")
    return cast(RepairTaskPayload, payload)


def _arguments(payload: RepairTaskPayload) -> list[str]:
    argv = [payload["operation"]]
    for key, flag in (
        ("repair_id", "--repair-id"),
        ("run_id", "--run-id"),
        ("owner_id", "--owner-id"),
        ("expected_revision", "--expected-revision"),
        ("approval_id", "--approval-id"),
        ("unit_id", "--unit-id"),
        ("authorization_reference", "--authorization-reference"),
        ("predecessor_transition_id", "--predecessor-transition-id"),
    ):
        value = payload.get(key)
        if value is not None:
            argv.extend((flag, str(value)))
    return argv
