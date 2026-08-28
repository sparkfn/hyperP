"""Strict shared execution-authority and atomic-unit protocol tests for issue #301."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import cast

import pytest
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import (
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildBudgetAuthorization,
    StandaloneCrmSourceChildEnvelope,
    StandaloneCrmSourceChildScope,
    StandaloneCrmSourceChildUnitAuthority,
    _SourceChildEnvelope,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmAtomicUnitRepository,
    StandaloneCrmUnitAccountingDelta,
)

_AUTHORIZATION_DIGEST = "sha256:" + "b" * 64
_PAYLOAD_DIGEST = "sha256:" + "a" * 64


def _authorization(
    unit: StandaloneCrmSourceChildUnitAuthority,
) -> StandaloneCrmSourceChildBudgetAuthorization:
    return StandaloneCrmSourceChildBudgetAuthorization(
        "authorization-a",
        _AUTHORIZATION_DIGEST,
        unit.census_id,
        unit.stream_kind,
        unit.generation,
        unit.fence_token,
        unit.fence_owner_id,
        unit.task_name,
        unit.task_id,
        unit.payload_digest,
        2,
        10,
        4,
        20,
        "2026-08-28T12:00:00Z",
        "2026-08-29T00:00:00Z",
    )


def _contact_envelope() -> ContactSourceChildEnvelope:
    scope = StandaloneCrmSourceChildScope("bitrix_chat", "portal-a", "control-a")
    unit = StandaloneCrmSourceChildUnitAuthority(
        "census-a", "contact", 2, 3, "worker-a", "task-a", "task-id-a", _PAYLOAD_DIGEST
    )
    authorization = _authorization(unit)
    return ContactSourceChildEnvelope(
        scope,
        unit,
        10,
        5,
        StandaloneCrmSourceAvailability("2026-08-28T00:00:00Z"),
        authorization,
        ContactBindingSubposition(6, 1),
    )


def _lead_envelope() -> LeadSourceChildEnvelope:
    scope = StandaloneCrmSourceChildScope("bitrix_chat", "portal-a", "control-a")
    unit = StandaloneCrmSourceChildUnitAuthority(
        "census-a", "lead", 2, 3, "worker-a", "task-a", "task-id-a", _PAYLOAD_DIGEST
    )
    authorization = _authorization(unit)
    return LeadSourceChildEnvelope(
        scope,
        unit,
        10,
        5,
        StandaloneCrmSourceAvailability("2026-08-28T00:00:00Z"),
        authorization,
    )


def test_source_child_authority_rejects_noncanonical_and_cross_domain_inputs() -> None:
    with pytest.raises(ValueError, match="canonical"):
        StandaloneCrmSourceChildScope("bitrix_chat", "Portal-A", "control-a")
    envelope = _contact_envelope()
    with pytest.raises(ValueError, match="cannot exceed"):
        ContactSourceChildEnvelope(
            envelope.scope,
            envelope.unit,
            10,
            11,
            envelope.availability,
            envelope.budget_authorization,
        )
    with pytest.raises(ValueError, match="stream kind"):
        LeadSourceChildEnvelope(
            envelope.scope,
            envelope.unit,
            10,
            5,
            envelope.availability,
            envelope.budget_authorization,
        )
    with pytest.raises(ValueError, match="canonical sha256"):
        StandaloneCrmSourceChildUnitAuthority(
            "census-a", "contact", 2, 3, "worker-a", "task-a", "task-id-a", "sha256:payload"
        )
    with pytest.raises(ValueError, match="canonical sha256"):
        replace(envelope.budget_authorization, authorization_digest="sha256:" + "A" * 64)
    with pytest.raises(ValueError, match="cannot exceed occurrence"):
        replace(
            envelope.budget_authorization,
            attempt_deadline="2026-08-28T00:00:00.100000Z",
            occurrence_deadline="2026-08-28T00:00:00Z",
        )
    for authorization in (
        replace(envelope.budget_authorization, fence_owner_id="worker-b"),
        replace(envelope.budget_authorization, task_name="task-b"),
        replace(envelope.budget_authorization, payload_digest="sha256:" + "c" * 64),
    ):
        with pytest.raises(ValueError, match="does not match"):
            ContactSourceChildEnvelope(
                envelope.scope,
                envelope.unit,
                envelope.frozen_upper_id,
                envelope.last_committed_id,
                envelope.availability,
                authorization,
            )


@dataclass(frozen=True)
class _Mutation:
    source_record_id: str


@dataclass(frozen=True)
class _Result:
    committed: bool


class _FakeAtomicRepository(StandaloneCrmAtomicUnitRepository[_Mutation, _Result]):
    def commit_unit(self, request: StandaloneCrmAtomicUnitCommit[_Mutation]) -> _Result:
        return _Result(committed=request.mutation.source_record_id == "record-a")


def test_atomic_unit_commit_binds_v1_checkpoint_and_component_mutation() -> None:
    envelope = _contact_envelope()
    expected = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 6, 1, 5, 1, 2, 3)
    proposed = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 7, 6, 2, 7, 2, 2, 3)
    request = StandaloneCrmAtomicUnitCommit(
        envelope,
        _Mutation("record-a"),
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(2, 1, 0),
    )
    assert _FakeAtomicRepository().commit_unit(request).committed
    with pytest.raises(ValueError, match="processed accounting"):
        StandaloneCrmAtomicUnitCommit(
            envelope,
            _Mutation("record-a"),
            expected,
            proposed,
            StandaloneCrmUnitAccountingDelta(1, 1, 0),
        )


def test_atomic_unit_commit_bounds_proposed_contact_binding_resume() -> None:
    envelope = _contact_envelope()
    expected = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 6, 1, 5, 1, 2, 3)
    resumed = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 6, 7, 0, 6, 1, 2, 3)

    request = StandaloneCrmAtomicUnitCommit(
        envelope,
        _Mutation("record-a"),
        expected,
        resumed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )

    assert request.proposed_checkpoint.binding_subject_id == 7
    with pytest.raises(ValueError, match="cannot exceed frozen upper bound"):
        StandaloneCrmAtomicUnitCommit(
            envelope,
            _Mutation("record-a"),
            expected,
            replace(resumed, binding_subject_id=11),
            StandaloneCrmUnitAccountingDelta(1, 0, 0),
        )


def test_atomic_unit_commit_rejects_checkpoint_cursor_or_binding_mismatch() -> None:
    envelope = _contact_envelope()
    proposed = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 6, 6, 2, 6, 0, 2, 3)
    stale_cursor = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 4, 6, 1, 4, 0, 2, 3)
    wrong_binding = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 6, 0, 5, 0, 2, 3)

    with pytest.raises(ValueError, match="cursor must equal"):
        StandaloneCrmAtomicUnitCommit(
            envelope,
            _Mutation("record-a"),
            stale_cursor,
            proposed,
            StandaloneCrmUnitAccountingDelta(2, 0, 0),
        )
    with pytest.raises(ValueError, match="binding position must equal"):
        StandaloneCrmAtomicUnitCommit(
            envelope,
            _Mutation("record-a"),
            wrong_binding,
            proposed,
            StandaloneCrmUnitAccountingDelta(1, 0, 0),
        )


def test_atomic_unit_commit_rejects_nonconcrete_authority_before_checkpoint_access() -> None:
    envelope = _SourceChildEnvelope(
        StandaloneCrmSourceChildScope("bitrix_chat", "portal-a", "control-a"),
        StandaloneCrmSourceChildUnitAuthority(
            "census-a", "contact", 2, 3, "worker-a", "task-a", "task-id-a", _PAYLOAD_DIGEST
        ),
        10,
        5,
        StandaloneCrmSourceAvailability("2026-08-28T00:00:00Z"),
        _authorization(
            StandaloneCrmSourceChildUnitAuthority(
                "census-a",
                "contact",
                2,
                3,
                "worker-a",
                "task-a",
                "task-id-a",
                _PAYLOAD_DIGEST,
            )
        ),
    )
    checkpoint = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, None, None, 5, 0, 2, 3)

    with pytest.raises(ValueError, match="concrete source child authority"):
        StandaloneCrmAtomicUnitCommit(
            cast(StandaloneCrmSourceChildEnvelope, envelope),
            _Mutation("record-a"),
            checkpoint,
            checkpoint,
            StandaloneCrmUnitAccountingDelta(0, 0, 0),
        )


def test_atomic_unit_commit_rejects_contact_binding_on_lead_checkpoints() -> None:
    envelope = _lead_envelope()
    expected = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, 5, 0, 5, 0, 2, 3)
    proposed = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 6, 5, 1, 6, 0, 2, 3)

    with pytest.raises(ValueError, match="lead and company checkpoints"):
        StandaloneCrmAtomicUnitCommit(
            envelope,
            _Mutation("record-a"),
            expected,
            proposed,
            StandaloneCrmUnitAccountingDelta(1, 0, 0),
        )
