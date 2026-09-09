"""Protected-evidence receipt codec and read-only verification tests."""

from __future__ import annotations

from typing import cast

import pytest
from intelligence.artifacts import canonical_json
from intelligence.crm.activities.cleanup.command_evidence import (
    _reconcile_activity_relationships_after_successful_calls,
    _relationship_digest_rows,
)
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.types import (
    AuthorizedCompanionRelationship,
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ProtectedSourceEndpointEvidence,
    QuiescenceEvidence,
    ResourceCeilings,
    canonical_digest,
)
from intelligence.crm.activities.cleanup.verification import verify_protected_evidence
from intelligence.repositories.protocols.crm_activity_cleanup import (
    CrmActivityCleanupRepository,
    EndpointIdentity,
    IncidentRelationship,
    ProtectedEvidence,
)


def _digest(value: str) -> str:
    return canonical_digest({"value": value})


def _quiescence() -> QuiescenceEvidence:
    return QuiescenceEvidence.create(
        "quiescence-run",
        "archive-run",
        "checkpoint-a",
        "snapshot-a",
        _digest("manifest"),
        _digest("cleanup"),
        "bitrix-source",
        "bitrix-a",
        "environment-a",
        "database-a",
        _digest("boundary"),
    )


def _source_endpoint(identity: CleanupIdentity) -> ProtectedSourceEndpointEvidence:
    return ProtectedSourceEndpointEvidence(
        identity.source_record_pk,
        f"from-source-{identity.source_record_pk}",
        "FROM_SOURCE",
        "outbound",
        "source-system-a",
        ("SourceSystem",),
        "bitrix-source",
    )


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
                "identifier-node-a",
                ("Identifier",),
                None,
                None,
                "nric",
                "opaque-identifier-node-a",
                None,
                None,
                None,
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
        _quiescence(),
        {"protected": 1},
        (identity,),
        (protected,),
        (_source_endpoint(identity),),
    )


def _companion_receipt() -> CleanupReceipt:
    receipt = _receipt()
    activity = receipt.identities[0]
    call = CleanupIdentity(
        "call-a",
        "call",
        activity.source_instance_id,
        activity.source_record_version,
        "call-hash",
        _digest("call-record"),
        _digest("call-references"),
        2,
        _digest("call-relations"),
        2,
        _digest("call-dependencies"),
    )
    return CleanupReceipt.create(
        receipt.cleanup_run_id,
        receipt.authorization,
        receipt.target,
        receipt.batch_size,
        receipt.resource_ceilings,
        receipt.policy_version,
        _quiescence(),
        dict(receipt.protected_baseline),
        (activity, call),
        protected_source_endpoints=(_source_endpoint(activity), _source_endpoint(call)),
        authorized_companion_relationships=(
            AuthorizedCompanionRelationship(
                "child-edge", "CHILD_OF", "call-a", "source-a", "outbound", "inbound"
            ),
            AuthorizedCompanionRelationship(
                "details-edge",
                "DETAILS_HISTORY_ITEM",
                "call-a",
                "source-a",
                "outbound",
                "inbound",
            ),
        ),
    )


def _activity_relationship(
    relationship_id: str, relationship_type: str, endpoint_pk: str
) -> IncidentRelationship:
    return IncidentRelationship(
        relationship_id,
        "inbound",
        relationship_type,
        EndpointIdentity(
            f"endpoint-{endpoint_pk}",
            ("SourceRecord",),
            endpoint_pk,
            None,
            None,
            None,
            None,
            None,
            None,
        ),
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
    proof = receipt.as_dict()["protected_preservation"]
    assert proof == {
        "schema_version": "crm-activities-cleanup-protected-preservation-v2",
        "scope": "exact-selected-from-source-endpoints-and-unowned-relationships",
        "selected_identity_count": 1,
        "selected_identity_digest": receipt.protected_preservation.selected_identity_digest,
        "relationship_count": 1,
        "relationship_digest": receipt.protected_preservation.relationship_digest,
        "source_endpoint_count": 1,
        "source_endpoint_digest": receipt.protected_preservation.source_endpoint_digest,
    }


def test_receipt_serialization_never_contains_raw_identifier_value() -> None:
    sentinel = "SENSITIVE-NRIC-RAW-VALUE"
    receipt = _receipt()
    serialized = canonical_json(receipt.as_dict())
    checkpoint_binding = canonical_json({"receipt": receipt.as_dict()})
    assert sentinel not in serialized
    assert sentinel not in checkpoint_binding
    assert "identifier_value" not in checkpoint_binding
    protected = receipt.as_dict()["protected_evidence"]
    assert isinstance(protected, list) and protected and isinstance(protected[0], dict)
    relationship = protected[0]["relationship"]
    assert isinstance(relationship, dict)
    endpoint = relationship["other_endpoint"]
    assert isinstance(endpoint, dict)
    assert endpoint["identifier_comparison_token"] == "opaque-identifier-node-a"
    assert "identifier_value" not in endpoint


def test_protected_preservation_proof_rejects_tampering() -> None:
    value = _receipt().as_dict()
    proof = value["protected_preservation"]
    assert isinstance(proof, dict)
    proof["relationship_count"] = 0
    with pytest.raises(ValueError, match="protected preservation proof"):
        CleanupReceipt.parse(value)


def test_source_endpoint_proof_is_exact_bounded_and_rejects_missing_or_tampered_evidence() -> None:
    receipt = _receipt()
    payload = receipt.as_dict()
    endpoints = payload["protected_source_endpoints"]
    assert isinstance(endpoints, list) and len(endpoints) == 1 and isinstance(endpoints[0], dict)
    endpoint = endpoints[0]
    assert endpoint["selected_source_record_pk"] == "source-a"
    assert endpoint["relationship_type"] == "FROM_SOURCE"
    assert endpoint["direction"] == "outbound"
    assert endpoint["endpoint_labels"] == ["SourceSystem"]

    missing = receipt.as_dict()
    missing["protected_source_endpoints"] = []
    with pytest.raises(ValueError, match="protected preservation proof"):
        CleanupReceipt.parse(missing)

    tampered = receipt.as_dict()
    tampered_endpoints = tampered["protected_source_endpoints"]
    assert isinstance(tampered_endpoints, list) and isinstance(tampered_endpoints[0], dict)
    tampered_endpoints[0]["endpoint_source_key"] = "other-source"
    proof = tampered["protected_preservation"]
    assert isinstance(proof, dict)
    proof["source_endpoint_digest"] = canonical_digest(tampered_endpoints)
    with pytest.raises(ValueError, match="quiescence"):
        CleanupReceipt.parse(tampered)


def test_source_endpoint_proof_excludes_raw_endpoint_properties() -> None:
    receipt = _receipt()
    payload = receipt.as_dict()
    serialized = canonical_json(payload)
    assert "SENSITIVE-SOURCE-ENDPOINT-PROPERTY" not in serialized
    endpoints = payload["protected_source_endpoints"]
    assert isinstance(endpoints, list) and isinstance(endpoints[0], dict)
    endpoints[0]["raw_source_uri"] = "SENSITIVE-SOURCE-ENDPOINT-PROPERTY"
    with pytest.raises(ValueError, match="schema"):
        CleanupReceipt.parse(payload)


def test_protected_verification_is_read_only_and_fails_closed() -> None:
    receipt = _receipt()
    repository = _Repository()
    verify_protected_evidence(cast(CrmActivityCleanupRepository, repository), receipt)
    assert repository.calls == 1
    with pytest.raises(RuntimeError, match="protected evidence"):
        verify_protected_evidence(
            cast(CrmActivityCleanupRepository, _Repository(receipt.protected_evidence)), receipt
        )


def test_prior_call_adjustment_allows_only_exact_authorized_parent_edge_removals() -> None:
    receipt = _companion_receipt()
    child = _activity_relationship("child-edge", "CHILD_OF", "call-a")
    details = _activity_relationship("details-edge", "DETAILS_HISTORY_ITEM", "call-a")
    unrelated = _activity_relationship("unrelated-edge", "LINKED_TO", "other-a")
    expected = tuple(sorted((child, details, unrelated), key=IncidentRelationship.key))
    adjusted = _reconcile_activity_relationships_after_successful_calls(
        receipt,
        "source-a",
        expected,
        (unrelated,),
        frozenset({"call-a"}),
    )
    assert adjusted == (unrelated,)
    with pytest.raises(RuntimeError, match="not an authorized"):
        _reconcile_activity_relationships_after_successful_calls(
            receipt,
            "source-a",
            expected,
            (),
            frozenset({"call-a"}),
        )


def test_receipt_relationship_digest_survives_only_proven_prior_call_removals() -> None:
    receipt = _companion_receipt()
    child = _activity_relationship("child-edge", "CHILD_OF", "call-a")
    details = _activity_relationship("details-edge", "DETAILS_HISTORY_ITEM", "call-a")
    unrelated = _activity_relationship("unrelated-edge", "LINKED_TO", "other-a")
    original = tuple(sorted((child, details, unrelated), key=IncidentRelationship.key))
    dry_rows, dry_virtual = _relationship_digest_rows(
        "source-a", original, receipt.authorized_companion_relationships, frozenset()
    )
    resumed_rows, resumed_virtual = _relationship_digest_rows(
        "source-a",
        (unrelated,),
        receipt.authorized_companion_relationships,
        frozenset({"call-a"}),
    )
    assert canonical_digest(dry_rows) == canonical_digest(resumed_rows)
    assert dry_virtual == 0
    assert resumed_virtual == 2
