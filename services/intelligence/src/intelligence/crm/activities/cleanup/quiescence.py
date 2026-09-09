"""State-registered admission for one operational CRM activity quiescence proof."""

from __future__ import annotations

from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.bounded import ReadBudget, ReadLimits, read_published_evidence
from intelligence.crm.activities.cleanup.types import (
    QuiescenceEvidence,
    require_digest,
    require_identifier,
)
from intelligence.models import OutputInventory, Run
from intelligence.registry import Cancelled, RegisteredCommand, Registry

_QUIESCENCE_COMMAND = "crm_activities_cleanup_quiescence"
_QUIESCENCE_LIMITS = ReadLimits(2_000_000, 64, 1)


class QuiescenceStateReader(Protocol):
    """The minimal State contract for admitting one immutable quiescence artifact."""

    def inspect(self, run_id: str) -> Run | None: ...

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]: ...


def quiescence_relative_path(quiescence_run_id: str) -> str:
    """Return the one fixed State-managed artifact path; no locator fallback exists."""
    require_identifier(quiescence_run_id, "quiescence_run_id")
    return f"quiescence/crm/activities/{quiescence_run_id}.json"


def admit_published_quiescence(
    workspace: Path,
    state: QuiescenceStateReader,
    quiescence_run_id: str,
    expected_evidence_digest: str,
) -> QuiescenceEvidence:
    """Fail closed unless State registers one canonical completed operational proof."""
    require_identifier(quiescence_run_id, "quiescence_run_id")
    require_digest(expected_evidence_digest, "expected quiescence evidence digest")
    run = state.inspect(quiescence_run_id)
    if run is None or run.state != "completed" or run.command != _QUIESCENCE_COMMAND:
        raise RuntimeError("cleanup quiescence runtime is not completed operational evidence")
    relative = quiescence_relative_path(quiescence_run_id)
    inventory = state.accepted_outputs(quiescence_run_id)
    expected_path = f"outputs/{quiescence_run_id}/{relative}"
    matching = tuple(item for item in inventory if item.relative_path == expected_path)
    if len(inventory) != 1 or len(matching) != 1:
        raise RuntimeError("cleanup quiescence State inventory is ambiguous")
    value, raw = read_published_evidence(
        workspace,
        quiescence_run_id,
        relative,
        ReadBudget(_QUIESCENCE_LIMITS),
    )
    if raw != canonical_json(value).encode("utf-8"):
        raise RuntimeError("cleanup quiescence bytes are noncanonical")
    registered = matching[0]
    if len(raw) != registered.byte_count or sha256(raw).hexdigest() != registered.sha256:
        raise RuntimeError("cleanup quiescence State hash or byte count conflicts")
    evidence = QuiescenceEvidence.parse(value)
    if (
        evidence.quiescence_run_id != quiescence_run_id
        or evidence.evidence_digest != expected_evidence_digest
    ):
        raise RuntimeError("cleanup quiescence evidence binding conflicts")
    return evidence


def registration_registry(template: QuiescenceEvidence) -> Registry:
    """Register one human-attested #390 proof using the actual runtime run identity."""
    return Registry(
        (
            RegisteredCommand(
                "crm_activities_cleanup_quiescence",
                True,
                partial(_write_registration, template),
                {"domain": "crm_activities_cleanup", "operation": "quiescence"},
            ),
        )
    )


def _write_registration(template: QuiescenceEvidence, staging: Path, cancelled: Cancelled) -> None:
    """Write spawn-safe evidence bound to the actual State-assigned run ID."""
    if cancelled():
        raise RuntimeError("quiescence evidence registration was cancelled")
    evidence = QuiescenceEvidence.create(
        staging.name,
        template.accepted_run_id,
        template.checkpoint_id,
        template.logical_snapshot_id,
        template.manifest_digest,
        template.cleanup_identity_digest,
        template.source_key,
        template.source_instance_id,
        template.environment_id,
        template.observed_database_identity,
        template.boundary_digest,
    )
    target = staging.joinpath(*quiescence_relative_path(staging.name).split("/"))
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.write_bytes(canonical_json(evidence.as_dict()).encode("utf-8"))
