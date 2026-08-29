"""Typed command and result contracts for one atomic CRM-deal repair mutation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from src.crm_deal_identity_repair.digests import (
    authority_evidence_digest,
    mutation_request_digest,
    mutation_result_digest,
    object_digest,
    outbox_event_digest,
    repaired_state_digest,
    rollback_image_digest,
)
from src.crm_deal_identity_repair.execution_models import (
    RepairCheckpoint,
    RepairFence,
    RepairMutationResult,
    RepairOutboxEvent,
    RepairRollbackImage,
    RepairUnit,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.models import JsonValue

MutationDisposition = Literal["applied", "review_required"]
MutationExecutionDecision = Literal["committed", "replayed", "drift_conflict", "rejected"]
ProvenanceClass = Literal[
    "independent_trusted",
    "reviewed_v2",
    "historical_deal_only",
    "self_supporting",
    "blocked_or_conflicting",
]
MutationFailureStage = Literal[
    "after_guard",
    "after_source_lock",
    "after_classification",
    "after_rollback_image",
    "after_source_record",
    "after_retirement",
    "after_decision",
    "after_staging",
    "after_ledger",
    "after_checkpoint",
    "after_outbox",
    "after_postcondition",
]


@dataclass(frozen=True)
class RepairAuthorityEvidence:
    """One current, transaction-locked ownership evidence classification."""

    person_id: str
    provenance_class: ProvenanceClass
    source_record_pks: tuple[str, ...]
    evidence_rows: tuple[dict[str, JsonValue], ...]

    def __post_init__(self) -> None:
        if not self.person_id:
            raise ValueError("repair authority person ID must be non-empty")
        if self.provenance_class not in {
            "independent_trusted",
            "reviewed_v2",
            "historical_deal_only",
            "self_supporting",
            "blocked_or_conflicting",
        }:
            raise ValueError("repair authority provenance class is invalid")
        if not self.source_record_pks or any(not value for value in self.source_record_pks):
            raise ValueError("repair authority evidence requires source record identities")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "person_id": self.person_id,
            "provenance_class": self.provenance_class,
            "source_record_pks": list(self.source_record_pks),
            "evidence_rows": [dict(row) for row in self.evidence_rows],
        }


def build_inventory_binding_digest(inventory: RepairInventoryItem) -> str:
    """Digest the exact immutable inventory row allocated to one repair unit."""
    return object_digest(
        b"crm-deal-identity-repair-unit-row-v1\x00",
        {
            "inventory_key": inventory.inventory_key,
            "source_record_pk": inventory.source_record_pk,
            "graph_fingerprint": inventory.graph_fingerprint,
            "stored_payload_fingerprint": inventory.stored_payload_fingerprint,
        },
    )


@dataclass(frozen=True)
class RepairMutationCommand:
    """All authority and frozen evidence bound to one single-unit transaction."""

    unit: RepairUnit
    fence: RepairFence
    inventory: RepairInventoryItem
    source_instance_id: str
    control_instance_id: str
    mutation_contract_version: str = "crm_deal_identity_repair_mutation_v1"

    def __post_init__(self) -> None:
        if self.inventory.partition == "negative_control":
            raise ValueError("negative-control inventory cannot become a mutation command")
        if (self.unit.run_id, self.unit.unit_id) != (self.fence.run_id, self.fence.unit_id):
            raise ValueError("repair unit and fence scope differs")
        if (
            self.unit.generation,
            self.unit.sequence,
            self.unit.attempt,
            self.unit.boundary_digest,
        ) != (
            self.fence.generation,
            self.fence.sequence,
            self.fence.attempt,
            self.fence.boundary_digest,
        ):
            raise ValueError("repair unit and fence authority differs")
        if self.fence.state != "claimed":
            raise ValueError("repair mutation requires a claimed fence")
        if not self.source_instance_id or not self.control_instance_id:
            raise ValueError("repair mutation source/control identity must be non-empty")
        if self.mutation_contract_version != "crm_deal_identity_repair_mutation_v1":
            raise ValueError("repair mutation contract version is invalid")
        expected_binding = (
            self.inventory.inventory_key,
            self.inventory.source_record_pk,
            self.inventory.graph_fingerprint,
            self.inventory.stored_payload_fingerprint,
            self.inventory_binding_digest,
        )
        actual_binding = (
            self.unit.inventory_key,
            self.unit.source_record_pk,
            self.unit.inventory_graph_fingerprint,
            self.unit.inventory_stored_payload_fingerprint,
            self.unit.inventory_binding_digest,
        )
        if actual_binding != expected_binding:
            raise ValueError("repair unit is not bound to the exact inventory row")

    @property
    def mutation_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.request_digest))

    @property
    def rollback_image_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.mutation_id + ":rollback"))

    @property
    def checkpoint_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.mutation_id + ":checkpoint"))

    @property
    def outbox_event_id(self) -> str:
        return str(uuid5(NAMESPACE_URL, self.mutation_id + ":outbox"))

    @property
    def request_digest(self) -> str:
        return mutation_request_digest(self.to_dict())

    @property
    def inventory_binding_digest(self) -> str:
        """Bind this allocated unit to exactly one immutable inventory row."""
        return build_inventory_binding_digest(self.inventory)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.unit.run_id,
            "unit_id": self.unit.unit_id,
            "generation": self.unit.generation,
            "sequence": self.unit.sequence,
            "attempt": self.unit.attempt,
            "owner_id": self.fence.owner_id,
            "fence_id": self.fence.fence_id,
            "fence_token": self.fence.token,
            "boundary_digest": self.unit.boundary_digest,
            "unit_fingerprint": self.unit.inventory_fingerprint,
            "inventory_key": self.inventory.inventory_key,
            "inventory_fingerprint": self.inventory.graph_fingerprint,
            "inventory_binding_digest": self.inventory_binding_digest,
            "stored_payload_fingerprint": self.inventory.stored_payload_fingerprint,
            "source_instance_id": self.source_instance_id,
            "control_instance_id": self.control_instance_id,
            "mutation_contract_version": self.mutation_contract_version,
        }


@dataclass(frozen=True)
class RepairMutationPlan:
    """A fully classified desired state, produced before the first domain write."""

    disposition: MutationDisposition
    source_record_payload: dict[str, JsonValue] | None
    source_record_pk: str
    source_record_version: int
    selected_person_id: str | None
    provisional_person_id: str | None
    current_owner_ids: tuple[str, ...]
    authority_evidence: tuple[RepairAuthorityEvidence, ...]
    reason_codes: tuple[str, ...]
    retired_source_record_pks: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.disposition == "applied" and self.source_record_payload is None:
            raise ValueError("applied repair plan requires a reconstructable v2 source record")
        if self.disposition == "applied" and not self.selected_person_id:
            raise ValueError("applied repair plan requires exactly one selected person")
        if self.disposition == "applied" and self.provisional_person_id is not None:
            raise ValueError("applied repair plan cannot have a provisional person")
        if self.disposition == "review_required" and self.selected_person_id is not None:
            raise ValueError("review repair plan cannot have an active selected person")
        if (
            self.provisional_person_id is not None
            and self.provisional_person_id not in self.current_owner_ids
        ):
            raise ValueError("provisional person must be an eligible current candidate")
        if self.source_record_version < 1 or not self.source_record_pk:
            raise ValueError("repair plan source version is invalid")
        if not self.reason_codes or any(not code for code in self.reason_codes):
            raise ValueError("repair plan requires reason codes")
        if len(self.retired_source_record_pks) != len(set(self.retired_source_record_pks)):
            raise ValueError("repair plan retirement source identities must be unique")

    @property
    def authority_digest(self) -> str:
        return authority_evidence_digest(
            {
                "current_owner_ids": list(self.current_owner_ids),
                "evidence": [item.to_dict() for item in self.authority_evidence],
            }
        )

    @property
    def external_authority_digest(self) -> str:
        """Digest authority that remains meaningful after self-contamination retirement."""
        external = tuple(
            item
            for item in self.authority_evidence
            if item.provenance_class not in {"historical_deal_only", "self_supporting"}
        )
        return authority_evidence_digest(
            {
                "current_owner_ids": list(self.current_owner_ids),
                "evidence": [item.to_dict() for item in external],
            }
        )

    def desired_state(self) -> dict[str, JsonValue]:
        return {
            "disposition": self.disposition,
            "source_record_pk": self.source_record_pk,
            "source_record_version": self.source_record_version,
            "selected_person_id": self.selected_person_id,
            "provisional_person_id": self.provisional_person_id,
            "retired_source_record_pks": list(self.retired_source_record_pks),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class RepairRollbackPayload:
    """Canonical full pre-state image, kept only in the repair ledger."""

    payload: dict[str, JsonValue]
    expected_repaired_state: dict[str, JsonValue]

    @property
    def image_digest(self) -> str:
        return rollback_image_digest(self.to_dict())

    @property
    def expected_repaired_digest(self) -> str:
        return repaired_state_digest(self.expected_repaired_state)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "payload": self.payload,
            "expected_repaired_state": self.expected_repaired_state,
        }


@dataclass(frozen=True)
class RepairAtomicMutationResult:
    """Typed response for committed, replayed, rejected, or drifted execution."""

    decision: MutationExecutionDecision
    mutation: RepairMutationResult | None
    rollback_image: RepairRollbackImage | None
    checkpoint: RepairCheckpoint | None
    outbox_event: RepairOutboxEvent | None
    repaired_state_digest: str | None

    @property
    def committed(self) -> bool:
        return self.decision == "committed"

    @property
    def replayed(self) -> bool:
        return self.decision == "replayed"


def build_result_digest(
    command: RepairMutationCommand,
    plan: RepairMutationPlan,
    rollback: RepairRollbackPayload,
) -> str:
    """Build one immutable result digest bound to request, authority, and state."""
    return mutation_result_digest(
        {
            "request_digest": command.request_digest,
            "authority_digest": plan.authority_digest,
            "rollback_image_digest": rollback.image_digest,
            "expected_repaired_digest": rollback.expected_repaired_digest,
            "desired_state": plan.desired_state(),
        }
    )


def build_outbox_digest(command: RepairMutationCommand, result_digest: str) -> str:
    """Build the bounded integration stub digest without rollback payload data."""
    return outbox_event_digest(
        {
            "run_id": command.unit.run_id,
            "unit_id": command.unit.unit_id,
            "mutation_id": command.mutation_id,
            "result_digest": result_digest,
        }
    )
