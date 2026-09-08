"""Bounded candidate history parsing and State-backed admission."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from intelligence.crm.activities.acceptance_descriptor import (
    AcceptanceDescriptor,
    PublicationPointer,
    bytes_digest,
    count,
    descriptor_relative_path,
    inventory,
    published_inventory,
    run_id,
)
from intelligence.crm.activities.bounded import (
    CANDIDATE_READ_LIMITS,
    ReadBudget,
    ReadLimits,
    checkpoint_root,
    read_evidence,
    read_published_evidence,
)
from intelligence.crm.activities.checkpoint_resume import request_digest
from intelligence.crm.activities.models import ArchiveRequest, validate_snapshot_id
from intelligence.models import OutputInventory, Run
from intelligence.runtime import IntelligenceRuntime

_SHA256 = frozenset("0123456789abcdef")
_PUBLICATION_PREFIX = "publication-candidate-"
_VERIFICATION_PREFIX = "verification-candidate-"


@dataclass(frozen=True)
class VerificationCandidate:
    """A child-written verification candidate tied to one accepted descriptor."""

    raw: dict[str, object]
    run_id: str
    accepted_run_id: str
    snapshot_id: str
    accepted_manifest_digest: str
    accepted_descriptor_sha256: str
    inventory: tuple[OutputInventory, ...]


def publication_candidate_name(run_id_value: str) -> str:
    """Return the immutable per-attempt publication-candidate evidence name."""
    run_id(run_id_value, "candidate run")
    return f"{_PUBLICATION_PREFIX}{sha256(run_id_value.encode('utf-8')).hexdigest()}.json"


def verification_candidate_name(run_id_value: str) -> str:
    """Return the immutable per-attempt verification-candidate evidence name."""
    run_id(run_id_value, "candidate run")
    return f"{_VERIFICATION_PREFIX}{sha256(run_id_value.encode('utf-8')).hexdigest()}.json"


def publication_candidate(pointer: PublicationPointer) -> dict[str, object]:
    """Build the intentionally minimal hidden publication candidate payload."""
    return pointer.as_dict()


def publication(
    runtime: IntelligenceRuntime,
    request: ArchiveRequest,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> dict[str, object] | None:
    """Return a State-registered descriptor only when current request admission agrees."""
    selected = accepted_publication(runtime, request.snapshot_id, limits)
    if selected is None:
        return None
    descriptor, _ = selected
    if descriptor.request != request or descriptor.request_digest != request_digest(request):
        return None
    return dict(descriptor.raw)


def verification(
    runtime: IntelligenceRuntime,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> dict[str, object] | None:
    """Return the completed verification candidate for the accepted descriptor, if any."""
    selected = accepted_publication(runtime, checkpoint_id, limits)
    if selected is None:
        return None
    descriptor, pointer = selected
    completed: list[VerificationCandidate] = []
    for candidate in read_verification_candidates(runtime.config.workspace, checkpoint_id, limits):
        state_run = runtime.state.inspect(candidate.run_id)
        if state_run is None or state_run.state != "completed":
            continue
        if (
            candidate.accepted_run_id != descriptor.run_id
            or candidate.snapshot_id != descriptor.snapshot_id
            or candidate.accepted_manifest_digest != descriptor.manifest_digest
            or candidate.accepted_descriptor_sha256 != pointer.descriptor_sha256
        ):
            raise RuntimeError("verification candidate conflicts with accepted descriptor")
        completed_with_inventory(runtime, state_run, candidate.inventory)
        completed.append(candidate)
    if not completed:
        return None
    return dict(first_by_created_at(runtime, completed).raw)


def accepted_publication(
    runtime: IntelligenceRuntime,
    checkpoint_id: str,
    limits: ReadLimits,
) -> tuple[AcceptanceDescriptor, PublicationPointer] | None:
    """Return a State-proven completed descriptor and its hidden pointer."""
    completed: list[tuple[AcceptanceDescriptor, PublicationPointer]] = []
    for pointer in read_publication_candidates(runtime.config.workspace, checkpoint_id, limits):
        state_run = runtime.state.inspect(pointer.run_id)
        if state_run is None or state_run.state != "completed":
            continue
        descriptor = registered_descriptor(runtime, pointer, state_run, limits)
        if descriptor.checkpoint_id != checkpoint_id:
            raise RuntimeError("publication descriptor checkpoint conflicts with candidate")
        completed.append((descriptor, pointer))
    if not completed:
        return None
    completed.sort(key=lambda item: created_at(runtime, item[0].run_id))
    return completed[0]


def read_publication_candidates(
    workspace: Path,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> tuple[PublicationPointer, ...]:
    """Read bounded immutable publication-candidate history without snapshot traversal."""
    return tuple(
        parse_publication(value, checkpoint_id)
        for _, value in candidate_history(workspace, checkpoint_id, _PUBLICATION_PREFIX, limits)
    )


def read_verification_candidates(
    workspace: Path,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> tuple[VerificationCandidate, ...]:
    """Read bounded immutable verification-candidate history without snapshot traversal."""
    return tuple(
        parse_verification(value, checkpoint_id)
        for _, value in candidate_history(workspace, checkpoint_id, _VERIFICATION_PREFIX, limits)
    )


def read_publication_candidate(
    workspace: Path,
    checkpoint_id: str,
    limits: ReadLimits = CANDIDATE_READ_LIMITS,
) -> PublicationPointer | None:
    """Compatibility read for a single unambiguous publication candidate."""
    candidates = read_publication_candidates(workspace, checkpoint_id, limits)
    return None if len(candidates) != 1 else candidates[0]


def parse_publication(value: Mapping[str, object], checkpoint_id: str) -> PublicationPointer:
    """Parse one minimal publication candidate pointer."""
    expected = {
        "run_id",
        "descriptor_relative_path",
        "descriptor_sha256",
        "descriptor_byte_count",
    }
    if set(value) != expected:
        raise ValueError("publication candidate schema is invalid")
    pointer = PublicationPointer(
        run_id(text(value, "run_id"), "candidate run"),
        text(value, "descriptor_relative_path"),
        digest(value.get("descriptor_sha256"), "descriptor sha256"),
        count(value, "descriptor_byte_count"),
    )
    if pointer.descriptor_relative_path != descriptor_relative_path(checkpoint_id):
        raise ValueError("publication candidate descriptor path is invalid")
    return pointer


def parse_verification(value: Mapping[str, object], checkpoint_id: str) -> VerificationCandidate:
    """Parse one verification candidate bound to a publication descriptor."""
    expected = {
        "checkpoint_id",
        "run_id",
        "accepted_run_id",
        "snapshot_id",
        "accepted_manifest_digest",
        "accepted_descriptor_sha256",
        "inventory",
    }
    if set(value) != expected or value.get("checkpoint_id") != checkpoint_id:
        raise ValueError("verification candidate schema is invalid")
    snapshot_id = text(value, "snapshot_id")
    validate_snapshot_id(snapshot_id)
    return VerificationCandidate(
        dict(value),
        run_id(text(value, "run_id"), "candidate run"),
        run_id(text(value, "accepted_run_id"), "accepted run"),
        snapshot_id,
        digest(value.get("accepted_manifest_digest"), "accepted manifest digest"),
        digest(value.get("accepted_descriptor_sha256"), "accepted descriptor sha256"),
        inventory(value, "inventory"),
    )


def registered_descriptor(
    runtime: IntelligenceRuntime,
    pointer: PublicationPointer,
    state_run: Run,
    limits: ReadLimits,
) -> AcceptanceDescriptor:
    """Read one descriptor and prove its complete output inventory through State."""
    expected_descriptor = OutputInventory(
        f"outputs/{pointer.run_id}/{pointer.descriptor_relative_path}",
        pointer.descriptor_sha256,
        pointer.descriptor_byte_count,
    )
    accepted_outputs = runtime.state.accepted_outputs(pointer.run_id)
    if expected_descriptor not in accepted_outputs:
        raise RuntimeError("publication descriptor is not registered in State")
    from intelligence.crm.activities.acceptance_descriptor import parse_descriptor

    metadata, raw = read_published_evidence(
        runtime.config.workspace,
        pointer.run_id,
        pointer.descriptor_relative_path,
        ReadBudget(limits),
    )
    if len(raw) != pointer.descriptor_byte_count or bytes_digest(raw) != pointer.descriptor_sha256:
        raise RuntimeError("publication descriptor bytes conflict with its candidate")
    descriptor = parse_descriptor(metadata)
    if descriptor.run_id != pointer.run_id or descriptor.command != state_run.command:
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
    return descriptor


def candidate_history(
    workspace: Path,
    checkpoint_id: str,
    prefix: str,
    limits: ReadLimits,
) -> tuple[tuple[str, dict[str, object]], ...]:
    root = checkpoint_root(workspace, checkpoint_id)
    if root is None:
        return ()
    budget = ReadBudget(limits)
    try:
        names = tuple(sorted(path.name for path in root.iterdir() if path.name.startswith(prefix)))
    except OSError as error:
        raise ValueError("candidate history could not be read") from error
    values: list[tuple[str, dict[str, object]]] = []
    for name in names:
        if not candidate_name(name, prefix):
            raise ValueError("candidate history name is invalid")
        value = read_evidence(root, name, budget)
        if value is None:
            raise ValueError("candidate history disappeared during read")
        add_inventory_rows(value, budget)
        values.append((name, value))
    return tuple(values)


def completed_with_inventory(
    runtime: IntelligenceRuntime, state_run: Run, values: tuple[OutputInventory, ...]
) -> None:
    if runtime.state.accepted_outputs(state_run.run_id) != published_inventory(
        state_run.run_id, values
    ):
        raise RuntimeError("candidate inventory conflicts with accepted outputs")


def first_by_created_at(
    runtime: IntelligenceRuntime, candidates: Iterable[VerificationCandidate]
) -> VerificationCandidate:
    return min(candidates, key=lambda item: created_at(runtime, item.run_id))


def created_at(runtime: IntelligenceRuntime, run_id_value: str) -> float:
    state_run = runtime.state.inspect(run_id_value)
    if state_run is None:
        raise RuntimeError("candidate run disappeared from State")
    return state_run.created_at


def candidate_name(value: str, prefix: str) -> bool:
    suffix = value.removeprefix(prefix).removesuffix(".json")
    return value == f"{prefix}{suffix}.json" and len(suffix) == 64 and set(suffix) <= _SHA256


def add_inventory_rows(value: Mapping[str, object], budget: ReadBudget) -> None:
    candidate_inventory = value.get("inventory")
    if isinstance(candidate_inventory, list):
        budget.add_rows(len(candidate_inventory))


def digest(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or set(value) - _SHA256:
        raise ValueError(f"{field} is invalid")
    return value


def text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result
