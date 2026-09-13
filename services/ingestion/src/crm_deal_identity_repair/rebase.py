"""Fail-closed authenticated allocation-boundary rebasing for issue #424."""

from __future__ import annotations

import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from src.crm_deal_identity_repair.control_models import RepairControlRequest, RepairDispatchLease
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import _digest, _identity, _nonnegative
from src.models import JsonValue

REBASE_RECEIPT_DOMAIN = b"crm-deal-identity-repair-rebase-receipt-v1\x00"
REBASE_AUDIT_DOMAIN = b"crm-deal-identity-repair-rebase-audit-v1\x00"
REBASE_HMAC_DOMAIN = b"crm-deal-identity-repair-rebase-hmac-v1\x00"


@dataclass(frozen=True)
class RepairBoundaryRebaseRequest:
    """The untrusted, non-executable identity of one requested boundary rebase."""

    control: RepairControlRequest
    approval_id: str
    fresh_artifact_id: str
    expected_observed_boundary_digest: str

    def __post_init__(self) -> None:
        _identity(self.approval_id, "rebase approval ID")
        _identity(self.fresh_artifact_id, "fresh rebase artifact ID")
        _digest(self.expected_observed_boundary_digest, "rebase observed boundary digest")

    @property
    def request_digest(self) -> str:
        return object_digest(REBASE_AUDIT_DOMAIN, self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "action": "rebase-boundary",
            "repair_id": self.control.repair_id,
            "run_id": self.control.run_id,
            "owner_id": self.control.owner_id,
            "token_digest": self.control.token_digest,
            "expected_revision": self.control.expected_revision,
            "approval_id": self.approval_id,
            "fresh_artifact_id": self.fresh_artifact_id,
            "expected_observed_boundary_digest": self.expected_observed_boundary_digest,
        }


@dataclass(frozen=True)
class RepairBoundaryRebaseResult:
    """Durable non-secret evidence returned by a successful/replayed rebase."""

    lease: RepairDispatchLease
    previous_boundary_digest: str
    replacement_boundary_digest: str
    receipt_digest: str
    audit_digest: str
    replayed: bool

    def __post_init__(self) -> None:
        _digest(self.previous_boundary_digest, "previous rebase boundary digest")
        _digest(self.replacement_boundary_digest, "replacement rebase boundary digest")
        _digest(self.receipt_digest, "rebase receipt digest")
        _digest(self.audit_digest, "rebase audit digest")


def rebase_receipt_digest(
    *,
    request_digest: str,
    run_id: str,
    completion_id: str,
    previous_boundary_digest: str,
    replacement_boundary_digest: str,
    revision: int,
) -> str:
    """Digest the replacement receipt without granting authority by itself."""
    return object_digest(
        REBASE_RECEIPT_DOMAIN,
        {
            "request_digest": request_digest,
            "run_id": run_id,
            "completion_id": completion_id,
            "previous_boundary_digest": previous_boundary_digest,
            "replacement_boundary_digest": replacement_boundary_digest,
            "revision": revision,
        },
    )


def rebase_audit_digest(
    *,
    request: RepairBoundaryRebaseRequest,
    completion_id: str,
    allocation_digest: str,
    unit_set_digest: str,
    fresh_artifact_manifest_hmac: str,
    fresh_inventory_digest: str,
    fresh_producer_repository_sha: str,
    fresh_producer_image_digest: str,
    previous_boundary_digest: str,
    replacement_boundary_digest: str,
    replacement_components: dict[str, JsonValue],
    previous_receipt_digest: str,
    previous_origin_hmac: str,
    revision: int,
) -> str:
    """Bind old authority, fresh evidence, allocation identity, and final seal."""
    _nonnegative(revision, "rebase revision")
    return object_digest(
        REBASE_AUDIT_DOMAIN,
        {
            **request.to_dict(),
            "completion_id": completion_id,
            "allocation_digest": allocation_digest,
            "unit_set_digest": unit_set_digest,
            "fresh_artifact_manifest_hmac": fresh_artifact_manifest_hmac,
            "fresh_inventory_digest": fresh_inventory_digest,
            "fresh_producer_repository_sha": fresh_producer_repository_sha,
            "fresh_producer_image_digest": fresh_producer_image_digest,
            "previous_boundary_digest": previous_boundary_digest,
            "replacement_boundary_digest": replacement_boundary_digest,
            "replacement_components": replacement_components,
            "previous_receipt_digest": previous_receipt_digest,
            "previous_origin_hmac": previous_origin_hmac,
            "revision": revision,
        },
    )


def rebase_hmac(*, secret: bytes, key_id: str, audit_digest: str) -> str:
    """Sign the audit digest with the existing approval key, domain separated."""
    if not secret or not key_id:
        raise ValueError("rebase signing configuration is missing")
    _digest(audit_digest, "rebase audit digest")
    return hmac.new(
        secret,
        REBASE_HMAC_DOMAIN + (key_id + "\x00" + audit_digest).encode("utf-8"),
        "sha256",
    ).hexdigest()


def validate_rebase_hmac(
    *, secret: bytes, key_id: str, audit_digest: str, supplied_hmac: str
) -> None:
    """Reject malformed or forged replacement-seal audit authentication."""
    if not isinstance(supplied_hmac, str) or len(supplied_hmac) != 64:
        raise RuntimeError("repair rebase audit HMAC is malformed")
    expected = rebase_hmac(secret=secret, key_id=key_id, audit_digest=audit_digest)
    if not hmac.compare_digest(supplied_hmac, expected):
        raise RuntimeError("repair rebase audit HMAC is invalid")


def required_rebase_string(values: Mapping[str, object], key: str) -> str:
    """Read a non-empty durable rebase property without silently coercing it."""
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError("repair rebase durable evidence is malformed")
    return value


def required_rebase_int(values: Mapping[str, object], key: str) -> int:
    value = values.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("repair rebase durable evidence is malformed")
    return value
