"""Protected-evidence receipt codec and read-only verification tests."""

from __future__ import annotations

import pytest
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.types import (
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.crm.activities.cleanup.verification import verify_protected_evidence
from intelligence.repositories.protocols.crm_activity_cleanup import (
    EndpointIdentity,
    IncidentRelationship,
    ProtectedEvidence,
)


def _digest(value: str) -> str:
    return canonical_digest({"value": value})


def _receipt() -> CleanupReceipt:
    identity = CleanupIdentity(
        "source-a",
        "crm_history",
        "bitrix-a",
        "v1",
        "hash-a",
        _digest("record"),
        _digest("references"),
        1,
        _digest("relations"),
        1,
        _digest("dependencies"),
    )
    protected = ProtectedEvidence(
        "source-a",
        IncidentRelationship(
            "relationship-a",
            "inbound",
            "LINKED_TO",
            EndpointIdentity(
                "person-a", ("Person",), None, "person-a", None, None, None, None, None
            ),
        ),
    )
    return CleanupReceipt.create(
        "cleanup-a",
        CleanupAuthorization(
            "checkpoint-a",
            "archive-run",
            "snapshot-a",
            _digest("manifest"),
            _digest("boundary"),
            _digest("cleanup"),
            "archive-db",
        ),
        CleanupTarget("environment-a", "environment-a", "database-a", "database-a"),
        1,
        ResourceCeilings(100_000, 100, 10, 10),
        "policy-v1",
        {"protected": 1},
        (identity,),
        (protected,),
    )


class _Repository:
    def __init__(self, missing: tuple[ProtectedEvidence, ...] = ()) -> None:
        self.missing = missing
        self.calls = 0

    def verify_protected(
        self, protected: tuple[ProtectedEvidence, ...]
    ) -> tuple[ProtectedEvidence, ...]:
        self.calls += 1
        assert protected == _receipt().protected_evidence
        return self.missing


def test_receipt_round_trips_exact_protected_relationship_evidence() -> None:
    receipt = _receipt()
    assert CleanupReceipt.parse(receipt.as_dict()) == receipt
    assert "relationship-a" in str(receipt.as_dict())


def test_protected_verification_is_read_only_and_fails_closed() -> None:
    receipt = _receipt()
    repository = _Repository()
    verify_protected_evidence(repository, receipt)
    assert repository.calls == 1
    with pytest.raises(RuntimeError, match="protected evidence"):
        verify_protected_evidence(_Repository(receipt.protected_evidence), receipt)
