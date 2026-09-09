"""Bounded global planning for manifest-authorized CRM activity cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from intelligence.repositories.protocols.crm_activity_cleanup import (
    MAX_BATCH_IDENTITIES,
    MAX_INCIDENT_RELATIONSHIPS,
    BatchOutcome,
    CleanupPlan,
    CrmActivityCleanupRepository,
    ExactRecordInspection,
    ExpectedDeletionFact,
    LiveTargetIdentity,
    ProtectedEvidence,
    RecordOutcome,
    RequiredAbsenceCrmActivityCleanupRepository,
)


class GlobalCleanupPlan(CleanupPlan):
    """A finite merged plan whose components were individually repository-bounded."""

    def __post_init__(self) -> None:
        expected = tuple(item.target.source_record_pk for item in self.expected_deletions)
        if expected != tuple(sorted(set(expected))):
            raise ValueError("global expected deletion identities are not canonical")
        protected = tuple(item.key() for item in self.protected_evidence)
        if protected != tuple(sorted(set(protected))):
            raise ValueError("global protected evidence is not canonical")
        outcomes = tuple(item.source_record_pk for item in self.outcomes)
        if outcomes != tuple(sorted(set(outcomes))):
            raise ValueError("global plan outcome identities are not canonical")


@dataclass(frozen=True)
class CleanupPlanning:
    """One bounded global planning result with exact component provenance."""

    identities: tuple[LiveTargetIdentity, ...]
    inspections: tuple[ExactRecordInspection, ...]
    plan: GlobalCleanupPlan
    components: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        identities = tuple(item.source_record_pk for item in self.identities)
        inspections = tuple(item.source_record_pk for item in self.inspections)
        outcomes = tuple(item.source_record_pk for item in self.plan.outcomes)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("global cleanup identities are not canonical")
        if inspections != identities or outcomes != identities:
            raise ValueError("global cleanup planning evidence does not exactly cover identities")
        flattened = tuple(key for component in self.components for key in component)
        if tuple(sorted(flattened)) != identities:
            raise ValueError("global cleanup components do not exactly cover identities")


class BoundedCleanupRepository(RequiredAbsenceCrmActivityCleanupRepository):
    """Delegate graph operations while never forwarding more than the actual batch ceiling."""

    def __init__(
        self,
        repository: CrmActivityCleanupRepository,
        chunk_size: int = MAX_BATCH_IDENTITIES,
    ) -> None:
        _require_chunk_size(chunk_size)
        self._repository = repository
        self._chunk_size = chunk_size

    def database_identity(self) -> str:
        return self._repository.database_identity()

    def inspect(
        self,
        source_record_pks: tuple[str, ...],
        relationship_limit: int = MAX_INCIDENT_RELATIONSHIPS,
    ) -> tuple[ExactRecordInspection, ...]:
        keys = _canonical_keys(source_record_pks, allow_empty=True)
        result: list[ExactRecordInspection] = []
        for chunk in _chunks(keys, self._chunk_size):
            inspected = (
                self._repository.inspect(chunk)
                if relationship_limit == MAX_INCIDENT_RELATIONSHIPS
                else self._repository.inspect(chunk, relationship_limit)
            )
            if tuple(item.source_record_pk for item in inspected) != chunk:
                raise RuntimeError("bounded cleanup inspection does not cover its exact chunk")
            result.extend(inspected)
        return tuple(result)

    def plan(
        self,
        identities: tuple[LiveTargetIdentity, ...],
        inspections: tuple[ExactRecordInspection, ...],
    ) -> CleanupPlan:
        ordered = tuple(sorted(identities, key=lambda item: item.source_record_pk))
        keys = _canonical_keys(tuple(item.source_record_pk for item in ordered), allow_empty=False)
        if len(keys) > self._chunk_size:
            raise ValueError("bounded cleanup planning requires a decomposed component")
        observed = tuple(sorted(inspections, key=lambda item: item.source_record_pk))
        if tuple(item.source_record_pk for item in observed) != keys:
            raise ValueError("bounded cleanup plan inspection coverage is invalid")
        return self._repository.plan(ordered, observed)

    def delete_batch(
        self,
        database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
    ) -> BatchOutcome:
        return self._repository.delete_batch(database_identity, expected)

    def delete_batch_with_required_absences(
        self,
        database_identity: str,
        expected: tuple[ExpectedDeletionFact, ...],
        required_absent: tuple[LiveTargetIdentity, ...],
    ) -> BatchOutcome:
        method = getattr(self._repository, "delete_batch_with_required_absences", None)
        if not callable(method):
            raise RuntimeError("cleanup repository lacks required-absence transaction support")
        repository = cast(RequiredAbsenceCrmActivityCleanupRepository, self._repository)
        return repository.delete_batch_with_required_absences(
            database_identity, expected, required_absent
        )

    def verify_protected(
        self, protected: tuple[ProtectedEvidence, ...]
    ) -> tuple[ProtectedEvidence, ...]:
        return self._repository.verify_protected(protected)

    def close(self) -> None:
        self._repository.close()


def plan_cleanup(
    identities: tuple[LiveTargetIdentity, ...],
    repository: CrmActivityCleanupRepository,
    maximum_identities: int,
    *,
    chunk_size: int = MAX_BATCH_IDENTITIES,
) -> CleanupPlanning:
    """Inspect and plan finite dependency components without losing global authorization."""
    _require_chunk_size(chunk_size)
    keys = _canonical_keys(tuple(item.source_record_pk for item in identities), allow_empty=True)
    if tuple(item.source_record_pk for item in identities) != keys:
        raise ValueError("accepted cleanup identities are not canonical")
    if maximum_identities < 1 or len(keys) > maximum_identities:
        raise RuntimeError("accepted cleanup identities exceed the admitted planning ceiling")
    if not identities:
        return CleanupPlanning((), (), GlobalCleanupPlan((), (), ()), ())
    groups = _dependency_groups(identities, chunk_size)
    by_key = {item.source_record_pk: item for item in identities}
    inspections: list[ExactRecordInspection] = []
    expected: list[ExpectedDeletionFact] = []
    protected: list[ProtectedEvidence] = []
    outcomes: list[RecordOutcome] = []
    for group in groups:
        selected = tuple(by_key[key] for key in group)
        observed = repository.inspect(group)
        if tuple(item.source_record_pk for item in observed) != group:
            raise RuntimeError("cleanup component inspection does not exactly cover identities")
        component_plan = repository.plan(selected, observed)
        _require_component_plan(group, component_plan)
        inspections.extend(observed)
        expected.extend(component_plan.expected_deletions)
        protected.extend(component_plan.protected_evidence)
        outcomes.extend(component_plan.outcomes)
    ordered_inspections = tuple(sorted(inspections, key=lambda item: item.source_record_pk))
    merged = GlobalCleanupPlan(
        tuple(sorted(expected, key=lambda item: item.target.source_record_pk)),
        tuple(sorted(protected, key=ProtectedEvidence.key)),
        tuple(sorted(outcomes, key=lambda item: item.source_record_pk)),
    )
    return CleanupPlanning(identities, ordered_inspections, merged, groups)


def _dependency_groups(
    identities: tuple[LiveTargetIdentity, ...], chunk_size: int
) -> tuple[tuple[str, ...], ...]:
    parent = {item.source_record_pk: item.source_record_pk for item in identities}

    def find(value: str) -> str:
        root = value
        while parent[root] != root:
            root = parent[root]
        while parent[value] != value:
            next_value = parent[value]
            parent[value] = root
            value = next_value
        return root

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for identity in identities:
        for related in (
            *identity.child_parent_source_record_pks,
            *identity.details_parent_source_record_pks,
        ):
            if related in parent:
                union(identity.source_record_pk, related)
    components: dict[str, list[str]] = {}
    for key in parent:
        components.setdefault(find(key), []).append(key)
    ordered = tuple(tuple(sorted(component)) for _, component in sorted(components.items()))
    groups: list[tuple[str, ...]] = []
    current: list[str] = []
    for component in ordered:
        if len(component) > chunk_size:
            raise RuntimeError("cleanup call-parent component exceeds repository batch ceiling")
        if current and len(current) + len(component) > chunk_size:
            groups.append(tuple(current))
            current = []
        current.extend(component)
    if current:
        groups.append(tuple(current))
    return tuple(groups)


def _require_component_plan(keys: tuple[str, ...], plan: CleanupPlan) -> None:
    outcomes = tuple(item.source_record_pk for item in plan.outcomes)
    if outcomes != keys:
        raise RuntimeError("cleanup component plan does not exactly partition identities")
    expected = tuple(item.target.source_record_pk for item in plan.expected_deletions)
    if not set(expected).issubset(keys):
        raise RuntimeError("cleanup component plan expands authorized identities")


def _canonical_keys(values: tuple[str, ...], *, allow_empty: bool) -> tuple[str, ...]:
    if not values:
        if allow_empty:
            return ()
        raise ValueError("cleanup identity collection is empty")
    if len(values) != len(set(values)) or any(not value for value in values):
        raise ValueError("cleanup identities are not finite and unique")
    return tuple(sorted(values))


def _chunks(values: tuple[str, ...], size: int) -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(values[index : index + size]) for index in range(0, len(values), size))


def _require_chunk_size(value: int) -> None:
    if not 1 <= value <= MAX_BATCH_IDENTITIES:
        raise ValueError("cleanup planning chunk size exceeds repository batch ceiling")
