"""Immutable non-executable repair boundary contracts and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from src.crm_deal_identity_repair.digests import object_digest
from src.models import JsonValue
from src.source_instances import canonical_source_instance_id

RepairBoundaryDriftReason = Literal[
    "missing_source_record",
    "source_instance_mismatch",
    "source_instance_disabled",
    "missing_binding",
    "missing_control_evidence",
    "binding_mismatch",
    "persisted_boundary_change",
]
RepairStopCondition = Literal[
    "boundary_drift",
    "task_presence",
    "fence_or_control_loss",
    "unexpected_source_version",
    "negative_control_change",
    "partial_mutation",
    "unexplained_equation",
    "rollback_verification_failure",
    "unauthorized_dispatch_state",
]

_STOP_CONDITIONS = frozenset(
    {
        "boundary_drift",
        "task_presence",
        "fence_or_control_loss",
        "unexpected_source_version",
        "negative_control_change",
        "partial_mutation",
        "unexplained_equation",
        "rollback_verification_failure",
        "unauthorized_dispatch_state",
    }
)
_MANIFEST_DOMAIN = b"crm-deal-identity-repair-execution-boundary-v1\x00"
_BOUNDARY_DOMAIN = b"crm-deal-identity-repair-persisted-boundary-v1\x00"
_RUN_IDENTITY_DOMAIN = b"crm-deal-identity-repair-qualification-identity-v1\x00"


@dataclass(frozen=True)
class RepairExecutionBoundaryManifest:
    """Complete immutable future boundary for one verified #254 artifact."""

    repair_id: str
    artifact_id: str
    artifact_manifest_hmac: str
    inventory_digest: str
    repository_sha: str
    image_digest: str
    configuration_digest: str
    source_contract_uuid: str
    environment: Literal["staging"]
    approval_reference: str
    unit_ceiling: int
    stop_conditions: tuple[str, ...]
    source_instance_id: str
    control_instance_id: str
    rollback_authority_reference: str
    rollback_authority_policy: str
    graph_boundary_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int
    execution_allowed: Literal[False] = False
    manifest_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_manifest_identity(self)
        _validate_manifest_counts(self)
        if self.execution_allowed is not False:
            raise ValueError("repair execution boundary must remain non-executable")
        if self.unit_ceiling > self.eligible_unit_count:
            raise ValueError("repair unit ceiling exceeds eligible inventory population")
        object.__setattr__(self, "manifest_digest", object_digest(_MANIFEST_DOMAIN, self.to_dict()))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "repair_id": self.repair_id,
            "artifact_id": self.artifact_id,
            "artifact_manifest_hmac": self.artifact_manifest_hmac,
            "inventory_digest": self.inventory_digest,
            "repository_sha": self.repository_sha,
            "image_digest": self.image_digest,
            "configuration_digest": self.configuration_digest,
            "source_contract_uuid": self.source_contract_uuid,
            "environment": self.environment,
            "approval_reference": self.approval_reference,
            "unit_ceiling": self.unit_ceiling,
            "stop_conditions": list(self.stop_conditions),
            "source_instance_id": self.source_instance_id,
            "control_instance_id": self.control_instance_id,
            "rollback_authority_reference": self.rollback_authority_reference,
            "rollback_authority_policy": self.rollback_authority_policy,
            "graph_boundary_digest": self.graph_boundary_digest,
            "inventory_row_count": self.inventory_row_count,
            "eligible_unit_count": self.eligible_unit_count,
            "negative_control_count": self.negative_control_count,
            "execution_allowed": self.execution_allowed,
        }

    @property
    def qualification_identity(self) -> str:
        return object_digest(
            _RUN_IDENTITY_DOMAIN,
            {"repair_id": self.repair_id, "manifest_digest": self.manifest_digest},
        )


@dataclass(frozen=True)
class RepairBoundarySnapshot:
    """Canonical graph/source/control evidence persisted inside Neo4j only."""

    source_instance_id: str
    control_instance_id: str
    inventory_source_record_pks: tuple[str, ...]
    inventory_digest: str
    inventory_row_count: int
    eligible_unit_count: int
    negative_control_count: int
    source_records_digest: str
    source_instance_digest: str
    control_digest: str
    boundary_digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_instance_ids(self.source_instance_id, self.control_instance_id)
        _validate_source_record_pks(self.inventory_source_record_pks)
        _validate_digest(self.inventory_digest, "current inventory digest")
        _validate_snapshot_counts(self)
        for value, label in (
            (self.source_records_digest, "source records digest"),
            (self.source_instance_digest, "source instance digest"),
            (self.control_digest, "control digest"),
        ):
            _validate_digest(value, label)
        object.__setattr__(self, "boundary_digest", object_digest(_BOUNDARY_DOMAIN, self.to_dict()))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_instance_id": self.source_instance_id,
            "control_instance_id": self.control_instance_id,
            "inventory_source_record_pks": list(self.inventory_source_record_pks),
            "inventory_digest": self.inventory_digest,
            "inventory_row_count": self.inventory_row_count,
            "eligible_unit_count": self.eligible_unit_count,
            "negative_control_count": self.negative_control_count,
            "source_records_digest": self.source_records_digest,
            "source_instance_digest": self.source_instance_digest,
            "control_digest": self.control_digest,
        }


def _validate_manifest_identity(manifest: RepairExecutionBoundaryManifest) -> None:
    _nonempty(manifest.repair_id, "repair ID")
    _validate_artifact_id(manifest.artifact_id)
    _validate_digest(manifest.artifact_manifest_hmac, "artifact manifest HMAC", prefixed=False)
    _validate_digest(manifest.inventory_digest, "inventory digest")
    _validate_repository_sha(manifest.repository_sha)
    _validate_digest(manifest.image_digest, "image digest")
    _validate_digest(manifest.configuration_digest, "configuration digest")
    _validate_uuid(manifest.source_contract_uuid)
    if manifest.environment != "staging":
        raise ValueError("repair execution boundary is staging-only")
    _bounded_nonsecret(manifest.approval_reference, "approval reference", 256)
    if isinstance(manifest.unit_ceiling, bool) or manifest.unit_ceiling <= 0:
        raise ValueError("repair unit ceiling must be positive")
    object.__setattr__(
        manifest, "stop_conditions", _canonical_stop_conditions(manifest.stop_conditions)
    )
    _validate_instance_ids(manifest.source_instance_id, manifest.control_instance_id)
    _bounded_nonsecret(manifest.rollback_authority_reference, "rollback authority reference", 256)
    _bounded_nonsecret(manifest.rollback_authority_policy, "rollback authority policy", 128)
    _validate_digest(manifest.graph_boundary_digest, "graph boundary digest")


def _validate_manifest_counts(manifest: RepairExecutionBoundaryManifest) -> None:
    counts = (
        manifest.inventory_row_count,
        manifest.eligible_unit_count,
        manifest.negative_control_count,
    )
    if any(isinstance(value, bool) or value < 0 for value in counts):
        raise ValueError("repair inventory counts must be non-negative")
    if (
        manifest.inventory_row_count
        != manifest.eligible_unit_count + manifest.negative_control_count
    ):
        raise ValueError("repair inventory counts are inconsistent")


def _validate_source_record_pks(values: tuple[str, ...]) -> None:
    if not values:
        raise ValueError("repair boundary requires inventory source records")
    if tuple(sorted(values)) != values:
        raise ValueError("repair boundary source record identities must be sorted")
    if len(set(values)) != len(values):
        raise ValueError("repair boundary source record identities must be unique")
    if any(not value for value in values):
        raise ValueError("repair boundary source record identity must be non-empty")


def _validate_snapshot_counts(snapshot: RepairBoundarySnapshot) -> None:
    counts = (
        snapshot.inventory_row_count,
        snapshot.eligible_unit_count,
        snapshot.negative_control_count,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("repair current inventory counts must be non-negative")
    if snapshot.inventory_row_count != len(snapshot.inventory_source_record_pks):
        raise ValueError("repair current inventory row count is inconsistent")
    if snapshot.inventory_row_count != sum(counts[1:]):
        raise ValueError("repair current inventory counts are inconsistent")


def _nonempty(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"repair {label} must be non-empty")


def _bounded_nonsecret(value: str, label: str, maximum: int) -> str:
    _nonempty(value, label)
    if len(value) > maximum or any(character in value for character in ("\x00", "\n", "\r")):
        raise ValueError(f"repair {label} is invalid")
    return value


def _canonical_stop_conditions(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values:
        raise ValueError("repair stop conditions must be non-empty")
    if len(values) != len(set(values)):
        raise ValueError("repair stop conditions must be unique")
    if any(value not in _STOP_CONDITIONS for value in values):
        raise ValueError("repair stop condition is unknown")
    return tuple(sorted(values))


def _validate_instance_ids(source_instance_id: str, control_instance_id: str) -> None:
    canonical_source_instance_id(source_instance_id, allow_legacy_default=True)
    canonical_source_instance_id(
        control_instance_id,
        field_name="control_instance_id",
        allow_legacy_default=True,
    )


def _validate_artifact_id(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("repair artifact ID must be lowercase UUID hex")


def _validate_digest(value: str, label: str, *, prefixed: bool = True) -> None:
    raw = value.removeprefix("sha256:") if prefixed else value
    if (prefixed and not value.startswith("sha256:")) or len(raw) != 64:
        raise ValueError(f"repair {label} must be a lowercase SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in raw):
        raise ValueError(f"repair {label} must be a lowercase SHA-256 digest")


def _validate_repository_sha(value: str) -> None:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("repair repository SHA must be lowercase 40-character hex")


def _validate_uuid(value: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("repair source-contract UUID is invalid") from exc
    if str(parsed) != value:
        raise ValueError("repair source-contract UUID must be canonical lowercase")
