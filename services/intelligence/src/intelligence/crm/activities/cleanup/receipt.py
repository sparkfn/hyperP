"""Canonical receipt codec and State-registered receipt admission."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal, Protocol, cast

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.bounded import ReadBudget, ReadLimits, read_published_evidence
from intelligence.crm.activities.cleanup.types import (
    RECEIPT_SCHEMA,
    CleanupAuthorization,
    CleanupIdentity,
    CleanupTarget,
    ResourceCeilings,
    canonical_digest,
    exact_keys,
    require_count,
    require_digest,
    require_identifier,
    require_mapping,
)
from intelligence.models import OutputInventory, Run
from intelligence.repositories.protocols.crm_activity_cleanup import (
    EndpointIdentity,
    IncidentRelationship,
    ProtectedEvidence,
)

_RECEIPT_COMMAND = "crm_activities_cleanup_dry_run"
_RECEIPT_LIMITS = ReadLimits(20_000_000, 10_000, 100_000)


class ReceiptStateReader(Protocol):
    """The State subset needed to admit one completed published receipt."""

    def inspect(self, run_id: str) -> Run | None: ...

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]: ...


@dataclass(frozen=True)
class CleanupReceipt:
    """Logical dry-run authorization, independent of runtime attempts and timestamps."""

    cleanup_run_id: str
    authorization: CleanupAuthorization
    target: CleanupTarget
    batch_size: int
    resource_ceilings: ResourceCeilings
    policy_version: str
    protected_baseline: tuple[tuple[str, int], ...]
    protected_evidence: tuple[ProtectedEvidence, ...]
    identities: tuple[CleanupIdentity, ...]
    identity_digest: str
    relationship_digest: str
    dependency_digest: str
    logical_digest: str

    def __post_init__(self) -> None:
        require_identifier(self.cleanup_run_id, "cleanup_run_id")
        if not 1 <= self.batch_size <= self.resource_ceilings.max_batch_size:
            raise ValueError("receipt batch_size is outside frozen ceiling")
        require_identifier(self.policy_version, "policy_version")
        if tuple(sorted(self.protected_baseline)) != self.protected_baseline:
            raise ValueError("protected baseline is not canonical")
        if any(
            not key or require_count(value, "protected baseline count") < 0
            for key, value in self.protected_baseline
        ):
            raise ValueError("protected baseline is invalid")
        if (
            tuple(sorted(set(self.protected_evidence), key=ProtectedEvidence.key))
            != self.protected_evidence
        ):
            raise ValueError("protected evidence is not canonical")
        keys = tuple(item.source_record_pk for item in self.identities)
        if self.identities != tuple(sorted(self.identities, key=_cleanup_order)) or len(
            set(keys)
        ) != len(keys):
            raise ValueError("receipt identities are not ordered and unique")
        if len(self.identities) > self.resource_ceilings.max_batches * self.batch_size:
            raise ValueError("receipt identities exceed frozen batch ceiling")
        for value, field in (
            (self.identity_digest, "identity_digest"),
            (self.relationship_digest, "relationship_digest"),
            (self.dependency_digest, "dependency_digest"),
            (self.logical_digest, "logical_digest"),
        ):
            require_digest(value, field)
        if self.logical_digest != canonical_digest(self.unsigned_dict()):
            raise ValueError("receipt logical digest is invalid")

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "schema_version": RECEIPT_SCHEMA,
            "cleanup_run_id": self.cleanup_run_id,
            "authorization": self.authorization.as_dict(),
            "target": self.target.as_dict(),
            "batch_size": self.batch_size,
            "resource_ceilings": self.resource_ceilings.as_dict(),
            "policy_version": self.policy_version,
            "protected_baseline": dict(self.protected_baseline),
            "protected_evidence": [_protected_dict(item) for item in self.protected_evidence],
            "identities": [item.as_dict() for item in self.identities],
            "identity_digest": self.identity_digest,
            "relationship_digest": self.relationship_digest,
            "dependency_digest": self.dependency_digest,
        }

    def as_dict(self) -> dict[str, object]:
        value = self.unsigned_dict()
        value["logical_digest"] = self.logical_digest
        return value

    @classmethod
    def create(
        cls,
        cleanup_run_id: str,
        authorization: CleanupAuthorization,
        target: CleanupTarget,
        batch_size: int,
        resource_ceilings: ResourceCeilings,
        policy_version: str,
        protected_baseline: Mapping[str, int],
        identities: Sequence[CleanupIdentity],
        protected_evidence: Sequence[ProtectedEvidence] = (),
    ) -> CleanupReceipt:
        require_identifier(cleanup_run_id, "cleanup_run_id")
        ordered = tuple(sorted(identities, key=_cleanup_order))
        baseline = tuple(
            sorted(
                (key, require_count(value, "protected baseline count"))
                for key, value in protected_baseline.items()
            )
        )
        protected = tuple(sorted(set(protected_evidence), key=ProtectedEvidence.key))
        identity_digest = canonical_digest([item.as_dict() for item in ordered])
        relationship_digest = canonical_digest(
            [
                {
                    "source_record_pk": item.source_record_pk,
                    "count": item.incident_relationship_count,
                    "digest": item.incident_relationship_digest,
                }
                for item in ordered
            ]
        )
        dependency_digest = canonical_digest(
            [
                {
                    "source_record_pk": item.source_record_pk,
                    "count": item.dependency_count,
                    "digest": item.dependency_digest,
                }
                for item in ordered
            ]
        )
        unsigned = {
            "schema_version": RECEIPT_SCHEMA,
            "cleanup_run_id": cleanup_run_id,
            "authorization": authorization.as_dict(),
            "target": target.as_dict(),
            "batch_size": batch_size,
            "resource_ceilings": resource_ceilings.as_dict(),
            "policy_version": policy_version,
            "protected_baseline": dict(baseline),
            "protected_evidence": [_protected_dict(item) for item in protected],
            "identities": [item.as_dict() for item in ordered],
            "identity_digest": identity_digest,
            "relationship_digest": relationship_digest,
            "dependency_digest": dependency_digest,
        }
        return cls(
            cleanup_run_id,
            authorization,
            target,
            batch_size,
            resource_ceilings,
            policy_version,
            baseline,
            protected,
            ordered,
            identity_digest,
            relationship_digest,
            dependency_digest,
            canonical_digest(unsigned),
        )

    @classmethod
    def parse(cls, value: object) -> CleanupReceipt:
        raw = require_mapping(value, "cleanup receipt")
        expected = frozenset(
            {
                "schema_version",
                "cleanup_run_id",
                "authorization",
                "target",
                "batch_size",
                "resource_ceilings",
                "policy_version",
                "protected_baseline",
                "protected_evidence",
                "identities",
                "identity_digest",
                "relationship_digest",
                "dependency_digest",
                "logical_digest",
            }
        )
        exact_keys(raw, expected, "cleanup receipt")
        if raw["schema_version"] != RECEIPT_SCHEMA:
            raise ValueError("cleanup receipt schema is unsupported")
        baseline = require_mapping(raw["protected_baseline"], "protected baseline")
        evidence = raw["protected_evidence"]
        identities = raw["identities"]
        if not isinstance(evidence, list) or not isinstance(identities, list):
            raise ValueError("receipt collections are invalid")
        return cls(
            require_identifier(raw["cleanup_run_id"], "cleanup_run_id"),
            CleanupAuthorization.parse(raw["authorization"]),
            CleanupTarget.parse(raw["target"]),
            require_count(raw["batch_size"], "batch_size"),
            ResourceCeilings.parse(raw["resource_ceilings"]),
            require_identifier(raw["policy_version"], "policy_version"),
            tuple(
                sorted(
                    (key, require_count(item, "protected baseline count"))
                    for key, item in baseline.items()
                )
            ),
            tuple(_protected_parse(item) for item in evidence),
            tuple(CleanupIdentity.parse(item) for item in identities),
            require_digest(raw["identity_digest"], "identity_digest"),
            require_digest(raw["relationship_digest"], "relationship_digest"),
            require_digest(raw["dependency_digest"], "dependency_digest"),
            require_digest(raw["logical_digest"], "logical_digest"),
        )


def _cleanup_order(value: CleanupIdentity) -> tuple[int, str]:
    """Keep companion calls ahead of activities across finite cleanup batches."""
    return (0 if value.record_type == "call" else 1, value.source_record_pk)


def receipt_relative_path(receipt_run_id: str) -> str:
    require_identifier(receipt_run_id, "receipt_run_id")
    return f"receipts/crm/activities/{receipt_run_id}.json"


def admit_published_receipt(
    workspace: Path, state: ReceiptStateReader, receipt_run_id: str, expected_logical_digest: str
) -> CleanupReceipt:
    """Admit only the exact State-registered completed receipt artifact."""
    require_identifier(receipt_run_id, "receipt_run_id")
    require_digest(expected_logical_digest, "expected receipt digest")
    run = state.inspect(receipt_run_id)
    if run is None or run.state != "completed" or run.command != _RECEIPT_COMMAND:
        raise RuntimeError("cleanup receipt runtime is not a completed dry-run")
    relative = receipt_relative_path(receipt_run_id)
    inventory = state.accepted_outputs(receipt_run_id)
    expected_path = f"outputs/{receipt_run_id}/{relative}"
    matching = tuple(item for item in inventory if item.relative_path == expected_path)
    if len(matching) != 1 or len(inventory) != 1:
        raise RuntimeError("cleanup receipt State inventory is ambiguous")
    value, raw = read_published_evidence(
        workspace, receipt_run_id, relative, ReadBudget(_RECEIPT_LIMITS)
    )
    if raw != canonical_json(value).encode("utf-8"):
        raise RuntimeError("cleanup receipt bytes are noncanonical")
    item = matching[0]
    if len(raw) != item.byte_count or sha256(raw).hexdigest() != item.sha256:
        raise RuntimeError("cleanup receipt State hash or byte count conflicts")
    receipt = CleanupReceipt.parse(value)
    if receipt.logical_digest != expected_logical_digest:
        raise RuntimeError("cleanup receipt logical digest conflicts")
    return receipt


def _protected_dict(value: ProtectedEvidence) -> dict[str, object]:
    relation = value.relationship
    endpoint = relation.other_endpoint
    return {
        "selected_source_record_pk": value.selected_source_record_pk,
        "relationship": {
            "relationship_element_id": relation.relationship_element_id,
            "direction": relation.direction,
            "relationship_type": relation.relationship_type,
            "other_endpoint": {
                "element_id": endpoint.element_id,
                "labels": list(endpoint.labels),
                "source_record_pk": endpoint.source_record_pk,
                "person_id": endpoint.person_id,
                "identifier_type": endpoint.identifier_type,
                "identifier_value": endpoint.identifier_value,
                "source_key": endpoint.source_key,
                "review_case_id": endpoint.review_case_id,
                "match_decision_id": endpoint.match_decision_id,
            },
        },
    }


def _protected_parse(value: object) -> ProtectedEvidence:
    raw = require_mapping(value, "protected evidence")
    exact_keys(raw, frozenset({"selected_source_record_pk", "relationship"}), "protected evidence")
    relation = require_mapping(raw["relationship"], "protected relationship")
    exact_keys(
        relation,
        frozenset({"relationship_element_id", "direction", "relationship_type", "other_endpoint"}),
        "protected relationship",
    )
    endpoint = require_mapping(relation["other_endpoint"], "protected endpoint")
    expected = frozenset(
        {
            "element_id",
            "labels",
            "source_record_pk",
            "person_id",
            "identifier_type",
            "identifier_value",
            "source_key",
            "review_case_id",
            "match_decision_id",
        }
    )
    exact_keys(endpoint, expected, "protected endpoint")
    labels = endpoint["labels"]
    if not isinstance(labels, list) or any(not isinstance(item, str) for item in labels):
        raise ValueError("protected endpoint labels are invalid")
    direction = require_identifier(relation["direction"], "protected relationship direction")
    if direction not in {"inbound", "outbound", "self"}:
        raise ValueError("protected relationship direction is invalid")
    return ProtectedEvidence(
        require_identifier(raw["selected_source_record_pk"], "selected_source_record_pk"),
        IncidentRelationship(
            require_identifier(relation["relationship_element_id"], "relationship_element_id"),
            cast(Literal["inbound", "outbound", "self"], direction),
            require_identifier(relation["relationship_type"], "relationship_type"),
            EndpointIdentity(
                require_identifier(endpoint["element_id"], "endpoint element_id"),
                tuple(labels),
                _optional(endpoint["source_record_pk"], "endpoint source_record_pk"),
                _optional(endpoint["person_id"], "endpoint person_id"),
                _optional(endpoint["identifier_type"], "endpoint identifier_type"),
                _optional(endpoint["identifier_value"], "endpoint identifier_value"),
                _optional(endpoint["source_key"], "endpoint source_key"),
                _optional(endpoint["review_case_id"], "endpoint review_case_id"),
                _optional(endpoint["match_decision_id"], "endpoint match_decision_id"),
            ),
        ),
    )


def _optional(value: object, field: str) -> str | None:
    if value is None:
        return None
    return require_identifier(value, field)
