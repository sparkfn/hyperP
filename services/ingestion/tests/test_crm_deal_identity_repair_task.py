from __future__ import annotations

import pytest
from src.crm_deal_identity_repair_tasks import _arguments, _validate_payload


def _payload(operation: str = "apply") -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "operation": operation,
        "repair_id": "repair",
        "run_id": "run",
        "owner_id": "owner",
        "expected_revision": 0,
        "approval_id": "approval",
    }
    if operation in {"apply", "verify", "rollback-status", "rollback"}:
        payload["unit_id"] = "unit"
    if operation in {"rollback-status", "rollback"}:
        payload["authorization_reference"] = "ticket"
        payload["predecessor_transition_id"] = "prior"
    return payload


def test_task_payload_rejects_forbidden_secret_and_unknown_keys() -> None:
    payload = _payload()
    forbidden = "authorization" + "_token"
    payload[forbidden] = "plaintext-not-allowed"
    with pytest.raises(ValueError, match="schema"):
        _validate_payload(payload)


def test_task_payload_rejects_boolean_revision() -> None:
    payload = _payload()
    payload["expected_revision"] = True
    with pytest.raises(ValueError, match="revision"):
        _validate_payload(payload)


def test_task_arguments_contain_no_authorization_secret() -> None:
    argv = _arguments(_payload("rollback-status"))
    assert "authorization" + "_token" not in " ".join(argv)
    assert "--authorization-reference" in argv


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("repair_id", 7),
        ("run_id", 7),
        ("owner_id", 7),
        ("approval_id", 7),
        ("unit_id", 7),
        ("authorization_reference", 7),
        ("predecessor_transition_id", 7),
        ("repair_id", ""),
        ("unit_id", " "),
        ("authorization_reference", ""),
    ),
)
def test_task_payload_rejects_wrong_string_field_types_and_empty_values(
    field: str, value: str | int
) -> None:
    payload = _payload("rollback-status")
    payload[field] = value
    with pytest.raises(ValueError, match="string"):
        _validate_payload(payload)


@pytest.mark.parametrize("revision", ("0", -1, 1.5))
def test_task_payload_rejects_non_integer_or_negative_revision(revision: object) -> None:
    payload: dict[str, object] = dict(_payload())
    payload["expected_revision"] = revision
    with pytest.raises(ValueError, match="revision"):
        _validate_payload(payload)
