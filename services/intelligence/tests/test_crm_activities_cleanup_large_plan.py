"""Bounded global planning contracts for large manifest-authorized cleanup sets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from intelligence.crm.activities.cleanup.planning import BoundedCleanupRepository, plan_cleanup
from intelligence.repositories.protocols.crm_activity_cleanup import (
    MAX_BATCH_IDENTITIES,
    BatchOutcome,
    CleanupPlan,
    ExactRecordInspection,
    ExpectedDeletionFact,
    LiveTargetIdentity,
    ObservedRecord,
    ParentIdentity,
    ProtectedEvidence,
    RecordOutcome,
)


def _identity(
    key: str,
    *,
    record_type: Literal["crm_history", "call"] = "crm_history",
    parents: tuple[str, ...] = (),
) -> LiveTargetIdentity:
    return LiveTargetIdentity(
        key,
        record_type,
        "instance-a",
        "version-a",
        f"hash-{key}",
        "active",
        "activity" if record_type == "crm_history" else None,
        ParentIdentity(None, None, None, None, None),
        "source-a",
        parents,
        parents,
    )


def _inspection(key: str, present: bool = False) -> ExactRecordInspection:
    record = (
        ObservedRecord(
            f"element-{key}",
            ("SourceRecord",),
            "crm_history",
            "instance-a",
            "version-a",
            f"hash-{key}",
            "active",
            "activity",
            ParentIdentity(None, None, None, None, None),
        )
        if present
        else None
    )
    return ExactRecordInspection(key, 1 if present else 0, record, (), 0, False)


@dataclass
class _Repository:
    present: frozenset[str] = frozenset()
    inspect_calls: list[tuple[str, ...]] = field(default_factory=list)
    plan_calls: list[tuple[str, ...]] = field(default_factory=list)

    def database_identity(self) -> str:
        return "database-a"

    def inspect(
        self, keys: tuple[str, ...], relationship_limit: int = 1_000
    ) -> tuple[ExactRecordInspection, ...]:
        assert relationship_limit == 1_000
        assert len(keys) <= MAX_BATCH_IDENTITIES
        assert keys == tuple(sorted(keys))
        self.inspect_calls.append(keys)
        return tuple(_inspection(key, key in self.present) for key in keys)

    def plan(
        self,
        identities: tuple[LiveTargetIdentity, ...],
        inspections: tuple[ExactRecordInspection, ...],
    ) -> CleanupPlan:
        keys = tuple(item.source_record_pk for item in identities)
        assert len(keys) <= MAX_BATCH_IDENTITIES
        assert keys == tuple(sorted(keys))
        assert tuple(item.source_record_pk for item in inspections) == keys
        self.plan_calls.append(keys)
        return CleanupPlan(
            (),
            (),
            tuple(RecordOutcome(key, "retained", "ready_for_batch_mutation") for key in keys),
        )

    def delete_batch(
        self, database_identity: str, expected: tuple[ExpectedDeletionFact, ...]
    ) -> BatchOutcome:
        del database_identity, expected
        raise AssertionError("planning test must not mutate")

    def verify_protected(
        self, protected: tuple[ProtectedEvidence, ...]
    ) -> tuple[ProtectedEvidence, ...]:
        del protected
        return ()

    def close(self) -> None:
        return None


def test_1001_independent_identities_are_inspected_and_planned_within_repository_limit() -> None:
    identities = tuple(_identity(f"identity-{index:04d}") for index in range(1_001))
    repository = _Repository()
    result = plan_cleanup(identities, repository, 1_001)
    assert [len(call) for call in repository.inspect_calls] == [1_000, 1]
    assert [len(call) for call in repository.plan_calls] == [1_000, 1]
    assert tuple(item.source_record_pk for item in result.plan.outcomes) == tuple(
        item.source_record_pk for item in identities
    )


def test_reverse_lexical_call_and_parent_are_kept_together_across_chunk_boundary() -> None:
    independent = tuple(_identity(f"a-{index:04d}") for index in range(999))
    parent = _identity("m-parent")
    call = _identity("z-call", record_type="call", parents=(parent.source_record_pk,))
    repository = _Repository()
    result = plan_cleanup((*independent, parent, call), repository, 1_001)
    assert result.components[-1] == ("m-parent", "z-call")
    assert repository.plan_calls[-1] == ("m-parent", "z-call")


def test_interleaved_components_are_sorted_within_groups_across_repository_boundary() -> None:
    parent = _identity("a-parent")
    independent = tuple(_identity(f"b-independent-{index:04d}") for index in range(999))
    call = _identity("z-call", record_type="call", parents=(parent.source_record_pk,))
    identities = (parent, *independent, call)
    repository = _Repository()

    result = plan_cleanup(identities, repository, len(identities))

    first = (
        parent.source_record_pk,
        *(item.source_record_pk for item in independent[:-1]),
        call.source_record_pk,
    )
    second = (independent[-1].source_record_pk,)
    assert result.components == (first, second)
    assert repository.inspect_calls == [first, second]
    assert repository.plan_calls == [first, second]
    assert all(
        values == tuple(sorted(values)) and len(values) <= MAX_BATCH_IDENTITIES
        for values in repository.inspect_calls
    )
    assert all(
        values == tuple(sorted(values)) and len(values) <= MAX_BATCH_IDENTITIES
        for values in repository.plan_calls
    )


def test_merged_global_evidence_is_deterministic_across_component_chunk_sizes() -> None:
    identities = tuple(_identity(f"a-{index:03d}") for index in range(19)) + (
        _identity("m-parent"),
        _identity("z-call", record_type="call", parents=("m-parent",)),
    )
    first = plan_cleanup(identities, _Repository(), len(identities), chunk_size=7)
    second = plan_cleanup(
        identities, _Repository(), len(identities), chunk_size=MAX_BATCH_IDENTITIES
    )
    assert first.identities == second.identities
    assert first.inspections == second.inspections
    assert first.plan == second.plan


def test_empty_authorized_snapshot_never_calls_repository() -> None:
    repository = _Repository()
    result = plan_cleanup((), repository, 1)
    assert result.identities == ()
    assert result.inspections == ()
    assert result.plan.outcomes == ()
    assert repository.inspect_calls == []
    assert repository.plan_calls == []


def test_chunked_final_absence_inspection_exposes_a_reappeared_identity() -> None:
    keys = tuple(f"identity-{index:04d}" for index in range(1_001))
    repository = _Repository(frozenset({keys[-1]}))
    bounded = BoundedCleanupRepository(repository)
    inspections = bounded.inspect(keys)
    assert [len(call) for call in repository.inspect_calls] == [1_000, 1]
    assert inspections[-1].source_record_pk == keys[-1]
    assert inspections[-1].matching_node_count == 1
