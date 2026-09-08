"""Bounded candidate history parsing and State-backed archive admission."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from intelligence.crm.activities.acceptance_descriptor import (
    AcceptanceDescriptor,
    PublicationPointer,
    bytes_digest,
    parse_descriptor,
    published_inventory,
)
from intelligence.crm.activities.acceptance_history_io import (
    accepted_outputs as _accepted_outputs,
)
from intelligence.crm.activities.acceptance_history_io import (
    candidate_history as _candidate_history,
)
from intelligence.crm.activities.acceptance_history_io import (
    inspect as _inspect,
)
from intelligence.crm.activities.acceptance_history_io import (
    inventory_key as _inventory_key,
)
from intelligence.crm.activities.acceptance_parsing import (
    PUBLICATION_PREFIX,
    VERIFICATION_PREFIX,
    VerificationCandidate,
    parse_publication,
    parse_verification,
    publication_candidate_name,
    verification_candidate_name,
)
from intelligence.crm.activities.bounded import (
    CANDIDATE_READ_LIMITS,
    ReadBudget,
    ReadLimits,
    read_published_evidence,
)
from intelligence.crm.activities.checkpoint_resume import request_digest
from intelligence.crm.activities.models import ArchiveRequest, sha256_json
from intelligence.models import OutputInventory, Run

_ARCHIVE_COMMANDS = frozenset({"crm_activities_extract", "crm_activities_resume"})
_VERIFICATION_COMMAND = "crm_activities_verify"


class _Config(Protocol):
    workspace: Path


class _State(Protocol):
    def inspect(self, run_id: str) -> Run | None: ...

    def accepted_outputs(self, run_id: str) -> tuple[OutputInventory, ...]: ...


class RuntimeReader(Protocol):
    @property
    def config(self) -> _Config: ...

    @property
    def state(self) -> _State: ...


@dataclass(frozen=True)
class Attempt:
    run_id: str
    state: str | None


@dataclass(frozen=True)
class PublicationHistory:
    accepted: tuple[AcceptanceDescriptor, PublicationPointer] | None
    attempts: tuple[Attempt, ...]


@dataclass(frozen=True)
class VerificationHistory:
    accepted: VerificationCandidate | None
    attempts: tuple[Attempt, ...]


def publication_candidate(pointer: PublicationPointer) -> dict[str, object]:
    """Build the intentionally minimal hidden publication candidate payload."""
    return pointer.as_dict()


def publication(
    runtime: RuntimeReader,
    request: ArchiveRequest,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> dict[str, object] | None:
    """Return only a completed descriptor matching the full current request."""
    selected = publication_history(runtime, request.snapshot_id, limits).accepted
    if selected is None:
        return None
    descriptor, _ = selected
    if descriptor.request != request or descriptor.request_digest != request_digest(request):
        return None
    return dict(descriptor.raw)


def verification(
    runtime: RuntimeReader,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> dict[str, object] | None:
    """Return a completed, registered verification artifact for this checkpoint."""
    budget = ReadBudget(limits)
    publication_value = _publication_history(runtime, checkpoint_id, budget)
    verification_value = _verification_history(runtime, checkpoint_id, publication_value, budget)
    return None if verification_value.accepted is None else dict(verification_value.accepted.raw)


def accepted_publication(
    runtime: RuntimeReader,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> tuple[AcceptanceDescriptor, PublicationPointer] | None:
    """Expose descriptor-backed checkpoint admission without source configuration."""
    return publication_history(runtime, checkpoint_id, limits).accepted


def publication_history(
    runtime: RuntimeReader,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> PublicationHistory:
    """Read all publication attempts through one non-resettable admission budget."""
    return _publication_history(runtime, checkpoint_id, ReadBudget(limits))


def status_history(
    runtime: RuntimeReader,
    checkpoint_id: str,
    budget: ReadBudget,
) -> tuple[PublicationHistory, VerificationHistory]:
    """Read both candidate histories through the caller's single status budget."""
    publication_value = _publication_history(runtime, checkpoint_id, budget)
    verification_value = _verification_history(runtime, checkpoint_id, publication_value, budget)
    return publication_value, verification_value


def verification_history(
    runtime: RuntimeReader,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> VerificationHistory:
    """Read verification attempts and their accepted artifact through one budget."""
    budget = ReadBudget(limits)
    publication_value = _publication_history(runtime, checkpoint_id, budget)
    return _verification_history(runtime, checkpoint_id, publication_value, budget)


def _publication_history(
    runtime: RuntimeReader,
    checkpoint_id: str,
    budget: ReadBudget,
) -> PublicationHistory:
    cache: dict[tuple[str, str, str, int], AcceptanceDescriptor] = {}
    attempts: list[Attempt] = []
    completed: list[tuple[AcceptanceDescriptor, PublicationPointer, Run]] = []
    seen: set[tuple[str, str, str, int]] = set()
    for name, value in _candidate_history(
        runtime.config.workspace,
        checkpoint_id,
        PUBLICATION_PREFIX,
        budget,
    ):
        pointer = parse_publication(value, checkpoint_id)
        if name != publication_candidate_name(pointer.run_id):
            raise ValueError("publication candidate name conflicts with its run identity")
        pointer_key = (
            pointer.run_id,
            pointer.descriptor_relative_path,
            pointer.descriptor_sha256,
            pointer.descriptor_byte_count,
        )
        if pointer_key in seen:
            continue
        seen.add(pointer_key)
        state_run = _inspect(runtime, pointer.run_id, budget)
        attempts.append(Attempt(pointer.run_id, None if state_run is None else state_run.state))
        if state_run is None or state_run.state != "completed":
            continue
        descriptor = _registered_descriptor(runtime, pointer, state_run, budget, cache)
        if descriptor.checkpoint_id != checkpoint_id:
            raise RuntimeError("publication descriptor checkpoint conflicts with candidate")
        completed.append((descriptor, pointer, state_run))
    selected: tuple[AcceptanceDescriptor, PublicationPointer] | None = None
    if completed:
        completed.sort(key=lambda item: item[2].created_at)
        selected = (completed[0][0], completed[0][1])
    return PublicationHistory(selected, tuple(attempts))


def _verification_history(
    runtime: RuntimeReader,
    checkpoint_id: str,
    publication_value: PublicationHistory,
    budget: ReadBudget,
) -> VerificationHistory:
    attempts: list[Attempt] = []
    completed: list[tuple[VerificationCandidate, Run]] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for name, value in _candidate_history(
        runtime.config.workspace,
        checkpoint_id,
        VERIFICATION_PREFIX,
        budget,
    ):
        candidate = parse_verification(value, checkpoint_id)
        if name != verification_candidate_name(candidate.run_id):
            raise ValueError("verification candidate name conflicts with its run identity")
        candidate_key = (
            candidate.run_id,
            candidate.accepted_run_id,
            candidate.snapshot_id,
            candidate.accepted_manifest_digest,
            candidate.accepted_descriptor_sha256,
            _inventory_key(candidate.inventory),
        )
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        state_run = _inspect(runtime, candidate.run_id, budget)
        attempts.append(Attempt(candidate.run_id, None if state_run is None else state_run.state))
        if state_run is None or state_run.state != "completed":
            continue
        if publication_value.accepted is None:
            raise RuntimeError("verification candidate has no accepted publication")
        descriptor, pointer = publication_value.accepted
        _validate_completed_verification(runtime, candidate, state_run, descriptor, pointer, budget)
        completed.append((candidate, state_run))
    selected: VerificationCandidate | None = None
    if completed:
        completed.sort(key=lambda item: item[1].created_at)
        selected = completed[0][0]
    return VerificationHistory(selected, tuple(attempts))


def read_publication_candidates(
    workspace: Path,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> tuple[PublicationPointer, ...]:
    """Read bounded publication pointer history for diagnostic callers."""
    budget = ReadBudget(limits)
    values: list[PublicationPointer] = []
    for name, value in _candidate_history(workspace, checkpoint_id, PUBLICATION_PREFIX, budget):
        pointer = parse_publication(value, checkpoint_id)
        if name != publication_candidate_name(pointer.run_id):
            raise ValueError("publication candidate name conflicts with its run identity")
        values.append(pointer)
    return tuple(values)


def read_verification_candidates(
    workspace: Path,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> tuple[VerificationCandidate, ...]:
    """Read bounded verification candidate history for diagnostic callers."""
    budget = ReadBudget(limits)
    values: list[VerificationCandidate] = []
    for name, value in _candidate_history(workspace, checkpoint_id, VERIFICATION_PREFIX, budget):
        candidate = parse_verification(value, checkpoint_id)
        if name != verification_candidate_name(candidate.run_id):
            raise ValueError("verification candidate name conflicts with its run identity")
        values.append(candidate)
    return tuple(values)


def read_publication_candidate(
    workspace: Path,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> PublicationPointer | None:
    """Compatibility read for one unambiguous publication candidate."""
    candidates = read_publication_candidates(workspace, checkpoint_id, limits)
    return None if len(candidates) != 1 else candidates[0]


def _registered_descriptor(
    runtime: RuntimeReader,
    pointer: PublicationPointer,
    state_run: Run,
    budget: ReadBudget,
    cache: dict[tuple[str, str, str, int], AcceptanceDescriptor],
) -> AcceptanceDescriptor:
    key = (
        pointer.run_id,
        pointer.descriptor_relative_path,
        pointer.descriptor_sha256,
        pointer.descriptor_byte_count,
    )
    cached = cache.get(key)
    if cached is not None:
        return cached
    expected_descriptor = OutputInventory(
        f"outputs/{pointer.run_id}/{pointer.descriptor_relative_path}",
        pointer.descriptor_sha256,
        pointer.descriptor_byte_count,
    )
    accepted_outputs = _accepted_outputs(runtime, pointer.run_id, budget)
    if expected_descriptor not in accepted_outputs:
        raise RuntimeError("publication descriptor is not registered in State")
    metadata, raw = read_published_evidence(
        runtime.config.workspace,
        pointer.run_id,
        pointer.descriptor_relative_path,
        budget,
    )
    if len(raw) != pointer.descriptor_byte_count or bytes_digest(raw) != pointer.descriptor_sha256:
        raise RuntimeError("publication descriptor bytes conflict with its candidate")
    descriptor = parse_descriptor(metadata)
    if (
        state_run.command not in _ARCHIVE_COMMANDS
        or descriptor.run_id != pointer.run_id
        or descriptor.command != state_run.command
    ):
        raise RuntimeError("publication descriptor run linkage is invalid")
    expected = tuple(
        sorted(
            (
                expected_descriptor,
                *published_inventory(pointer.run_id, descriptor.snapshot_inventory),
            ),
            key=lambda item: item.relative_path,
        )
    )
    if accepted_outputs != expected:
        raise RuntimeError("publication descriptor inventory conflicts with accepted State outputs")
    cache[key] = descriptor
    return descriptor


def _validate_completed_verification(
    runtime: RuntimeReader,
    candidate: VerificationCandidate,
    state_run: Run,
    descriptor: AcceptanceDescriptor,
    pointer: PublicationPointer,
    budget: ReadBudget,
) -> None:
    if state_run.command != _VERIFICATION_COMMAND:
        raise RuntimeError("verification candidate run command is invalid")
    if (
        candidate.accepted_run_id != descriptor.run_id
        or candidate.snapshot_id != descriptor.snapshot_id
        or candidate.accepted_manifest_digest != descriptor.manifest_digest
        or candidate.accepted_descriptor_sha256 != pointer.descriptor_sha256
    ):
        raise RuntimeError("verification candidate conflicts with accepted descriptor")
    relative_path = f"verifications/crm/activities/{descriptor.snapshot_id}.json"
    if len(candidate.inventory) != 1 or candidate.inventory[0].relative_path != relative_path:
        raise RuntimeError("verification candidate inventory is invalid")
    expected = published_inventory(state_run.run_id, candidate.inventory)
    if _accepted_outputs(runtime, state_run.run_id, budget) != expected:
        raise RuntimeError("verification candidate inventory conflicts with accepted outputs")
    evidence, raw = read_published_evidence(
        runtime.config.workspace,
        state_run.run_id,
        relative_path,
        budget,
    )
    artifact = candidate.inventory[0]
    if len(raw) != artifact.byte_count or bytes_digest(raw) != artifact.sha256:
        raise RuntimeError("verification artifact bytes conflict with its candidate")
    expected_keys = {
        "schema_version",
        "snapshot_id",
        "manifest_digest",
        "verified",
        "accepted_run_id",
        "accepted_manifest_digest",
        "accepted_descriptor_sha256",
        "digest",
    }
    unsigned = dict(evidence)
    artifact_digest = unsigned.pop("digest", None)
    if (
        set(evidence) != expected_keys
        or evidence.get("schema_version") != "crm-activities-verification-v1"
        or evidence.get("snapshot_id") != descriptor.snapshot_id
        or evidence.get("manifest_digest") != descriptor.manifest_digest
        or evidence.get("verified") is not True
        or evidence.get("accepted_run_id") != descriptor.run_id
        or evidence.get("accepted_manifest_digest") != descriptor.manifest_digest
        or evidence.get("accepted_descriptor_sha256") != pointer.descriptor_sha256
        or not isinstance(artifact_digest, str)
        or sha256_json(unsigned) != artifact_digest
    ):
        raise RuntimeError("verification artifact is not bound to accepted descriptor")
