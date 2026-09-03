"""Strict digest-only contracts for the CRM-deal repair integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import _digest, _identity
from src.models import JsonValue

IntegrationOperation = Literal[
    "apply",
    "verify",
    "rollback-status",
    "rollback",
    "accept",
    "release-dispatch",
]
_UNIT_OPERATIONS = frozenset({"apply", "verify", "rollback-status", "rollback"})
_ROLLBACK_OPERATIONS = frozenset({"rollback-status", "rollback"})


@dataclass(frozen=True)
class RepairIntegrationRequest:
    """Untrusted command identity with no plaintext authorization material."""

    operation: IntegrationOperation
    control: RepairControlRequest
    approval_id: str
    unit_id: str | None = None
    authorization_reference: str | None = None
    predecessor_transition_id: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in _UNIT_OPERATIONS | {"accept", "release-dispatch"}:
            raise ValueError("repair integration operation is invalid")
        _identity(self.approval_id, "integration approval ID")
        if (self.operation in _UNIT_OPERATIONS) != (self.unit_id is not None):
            raise ValueError("repair integration unit scope is invalid")
        if self.unit_id is not None:
            _identity(self.unit_id, "integration unit ID")
        evidence = (self.authorization_reference, self.predecessor_transition_id)
        if self.operation in _ROLLBACK_OPERATIONS:
            if any(value is None for value in evidence):
                raise ValueError("repair rollback authorization evidence is required")
            for value in evidence:
                assert value is not None
                _identity(value, "rollback authorization evidence", maximum=200)
        elif any(value is not None for value in evidence):
            raise ValueError("rollback evidence is invalid for this operation")

    @property
    def request_digest(self) -> str:
        return object_digest(
            b"crm-deal-identity-repair-integration-request-v1\x00",
            self.to_dict(),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "operation": self.operation,
            "repair_id": self.control.repair_id,
            "run_id": self.control.run_id,
            "owner_id": self.control.owner_id,
            "token_digest": self.control.token_digest,
            "expected_revision": self.control.expected_revision,
            "approval_id": self.approval_id,
            "unit_id": self.unit_id,
            "authorization_reference": self.authorization_reference,
            "predecessor_transition_id": self.predecessor_transition_id,
        }


@dataclass(frozen=True)
class RepairIntegrationReceipt:
    """Safe report result. It deliberately contains only non-secret digests."""

    operation: IntegrationOperation
    request_digest: str
    receipt_digest: str
    state: str

    def __post_init__(self) -> None:
        _digest(self.request_digest, "integration request digest")
        _digest(self.receipt_digest, "integration receipt digest")
        _identity(self.state, "integration receipt state")

    @classmethod
    def create(
        cls,
        operation: IntegrationOperation,
        request_digest: str,
        state: str,
    ) -> RepairIntegrationReceipt:
        receipt_digest = object_digest(
            b"crm-deal-identity-repair-integration-receipt-v1\x00",
            {
                "operation": operation,
                "request_digest": request_digest,
                "state": state,
            },
        )
        return cls(operation, request_digest, receipt_digest, state)


def rollback_status_receipt_digest(
    *,
    run_id: str,
    unit_id: str,
    receipt_id: str,
    fence_id: str,
    mutation_id: str,
    image_digest: str,
    authorization_transition_id: str,
    authorization_digest: str,
    status_digest: str,
    control_revision: int,
    allocation_revision: int,
    completion_id: str,
    generation: int,
    sequence: int,
    attempt: int,
) -> str:
    """Digest the complete immutable, non-secret rollback-status receipt authority."""
    return object_digest(
        b"crm-deal-identity-repair-rollback-status-receipt-v1\x00",
        {
            "run_id": run_id,
            "unit_id": unit_id,
            "receipt_id": receipt_id,
            "fence_id": fence_id,
            "mutation_id": mutation_id,
            "image_digest": image_digest,
            "authorization_transition_id": authorization_transition_id,
            "authorization_digest": authorization_digest,
            "status_digest": status_digest,
            "control_revision": control_revision,
            "allocation_revision": allocation_revision,
            "completion_id": completion_id,
            "generation": generation,
            "sequence": sequence,
            "attempt": attempt,
        },
    )
