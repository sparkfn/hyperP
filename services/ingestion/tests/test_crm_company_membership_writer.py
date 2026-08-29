"""Boundary validation for the A-S2 company-membership writer."""

from __future__ import annotations

from dataclasses import replace

import pytest
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import (
    CrmCompanyDescriptionHead,
    CrmCompanyDescriptionHeadCompareAndSet,
    CrmCompanyMembershipHead,
    CrmCompanyMembershipHeadCompareAndSet,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
    CrmCompanyReference,
)
from src.crm_company_membership_writer import (
    CrmCompanyDescriptionMutation,
    CrmCompanyMembershipMutation,
    build_company_description_commit,
    build_company_membership_commit,
    membership_company_reference,
)
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import StandaloneCrmSourceAvailability
from src.standalone_crm_unit_repository import StandaloneCrmUnitAccountingDelta
from tests._standalone_crm_lane_a_fakes import (
    company_description,
    company_envelope,
    contact_envelope,
    lead_envelope,
    source_availability,
    source_scope,
)


def _checkpoint(
    stream: str,
    *,
    cursor: int = 5,
    processed: int = 0,
    skipped: int = 0,
    binding_subject: int | None = None,
    binding_offset: int | None = None,
) -> StandaloneCrmCheckpoint:
    return StandaloneCrmCheckpoint(
        "census-a",
        stream,  # type: ignore[arg-type]
        10,
        None,
        cursor,
        binding_subject,
        binding_offset,
        processed,
        skipped,
        1,
        2,
    )


def _snapshot(
    *,
    subject_type: str = "contact",
    payloads: tuple[CrmCompanyBindingPayload, ...] = (),
    availability: StandaloneCrmSourceAvailability | None = None,
) -> CrmCompanyMembershipSnapshotRecord:
    normalized = normalize_company_membership_snapshot(
        subject_type=subject_type,  # type: ignore[arg-type]
        subject_id="5",
        payloads=payloads,
    )
    return CrmCompanyMembershipSnapshotRecord(
        source_scope(),
        normalized,
        f"bitrix-crm-{subject_type}-5",
        f"{subject_type}-record-5",
        1,
        f"{subject_type}-hash-5",
        "2026-08-27T00:00:00Z",
        availability or source_availability(),
        len(normalized.bindings),
    )


def _membership_mutation(
    snapshot: CrmCompanyMembershipSnapshotRecord,
) -> CrmCompanyMembershipMutation:
    observations = tuple(
        CrmCompanyMembershipObservation(
            snapshot,
            membership_company_reference(snapshot, binding.company_id),
            binding.sort,
            binding.role_id,
            binding.is_primary,
        )
        for binding in snapshot.membership_snapshot.bindings
    )
    head = CrmCompanyMembershipHead(
        snapshot.scope,
        snapshot.subject_type,
        snapshot.subject_id,
        snapshot,
    )
    return CrmCompanyMembershipMutation(
        snapshot,
        observations,
        CrmCompanyMembershipHeadCompareAndSet(None, head),
    )


@pytest.mark.parametrize("subject_type", ("contact", "lead"))
def test_complete_empty_membership_is_a_valid_durable_mutation(subject_type: str) -> None:
    snapshot = _snapshot(subject_type=subject_type)
    mutation = _membership_mutation(snapshot)
    envelope = contact_envelope() if subject_type == "contact" else lead_envelope()
    binding = (5, 0) if subject_type == "contact" else (None, None)
    expected = _checkpoint(
        subject_type,
        binding_subject=binding[0],
        binding_offset=binding[1],
    )
    proposed = _checkpoint(
        subject_type,
        processed=1,
        binding_subject=binding[0],
        binding_offset=binding[1],
    )

    commit = build_company_membership_commit(
        envelope,
        mutation,
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )

    assert commit.mutation.observations == ()
    assert commit.mutation.snapshot_record.binding_count == 0


def test_membership_observations_must_cover_canonical_bindings_in_order() -> None:
    snapshot = _snapshot(
        payloads=(
            CrmCompanyBindingPayload("4", 1, "8", "N"),
            CrmCompanyBindingPayload("3", 0, "7", "Y"),
        )
    )
    valid = _membership_mutation(snapshot)

    with pytest.raises(ValueError, match="cover all complete bindings"):
        CrmCompanyMembershipMutation(snapshot, valid.observations[:-1], valid.compare_and_set)
    with pytest.raises(ValueError, match="company is not canonical"):
        CrmCompanyMembershipMutation(
            snapshot,
            tuple(reversed(valid.observations)),
            valid.compare_and_set,
        )


def test_membership_rejects_noncanonical_reference_identity() -> None:
    snapshot = _snapshot(payloads=(CrmCompanyBindingPayload("3", 0, "7", "Y"),))
    head = CrmCompanyMembershipHead(
        snapshot.scope,
        snapshot.subject_type,
        snapshot.subject_id,
        snapshot,
    )
    observation = CrmCompanyMembershipObservation(
        snapshot,
        CrmCompanyReference(snapshot.scope, "3", "invented-reference"),
        0,
        "7",
        True,
    )

    with pytest.raises(ValueError, match="canonical company reference"):
        CrmCompanyMembershipMutation(
            snapshot,
            (observation,),
            CrmCompanyMembershipHeadCompareAndSet(None, head),
        )


def test_equal_or_reverse_head_transition_is_rejected_before_repository() -> None:
    snapshot = _snapshot()
    head = CrmCompanyMembershipHead(
        snapshot.scope,
        snapshot.subject_type,
        snapshot.subject_id,
        snapshot,
    )

    with pytest.raises(ValueError, match="must advance"):
        CrmCompanyMembershipMutation(
            snapshot,
            (),
            CrmCompanyMembershipHeadCompareAndSet(head, head),
        )


def test_membership_requires_exact_scope_availability_stream_and_bound() -> None:
    expected = _checkpoint("contact", binding_subject=5, binding_offset=0)
    proposed = _checkpoint("contact", processed=1, binding_subject=5, binding_offset=1)
    delta = StandaloneCrmUnitAccountingDelta(1, 0, 0)

    wrong_clock = _membership_mutation(
        _snapshot(availability=StandaloneCrmSourceAvailability("2026-08-28T00:00:01Z"))
    )
    with pytest.raises(ValueError, match="scope and availability"):
        build_company_membership_commit(contact_envelope(), wrong_clock, expected, proposed, delta)

    lead = _membership_mutation(_snapshot(subject_type="lead"))
    with pytest.raises(ValueError, match="subject type"):
        build_company_membership_commit(contact_envelope(), lead, expected, proposed, delta)

    out_of_bound = replace(
        _snapshot(),
        membership_snapshot=normalize_company_membership_snapshot(
            subject_type="contact", subject_id="11", payloads=()
        ),
        source_record_id="bitrix-crm-contact-11",
        source_record_pk="contact-record-11",
    )
    with pytest.raises(ValueError, match="frozen upper bound"):
        build_company_membership_commit(
            contact_envelope(),
            _membership_mutation(out_of_bound),
            expected,
            proposed,
            delta,
        )


def test_contact_position_must_be_exact_and_cannot_infer_final_cursor() -> None:
    mutation = _membership_mutation(_snapshot())
    delta = StandaloneCrmUnitAccountingDelta(1, 0, 0)
    expected = _checkpoint("contact", binding_subject=5, binding_offset=0)

    with pytest.raises(ValueError, match="parent-issued position"):
        build_company_membership_commit(
            contact_envelope(),
            mutation,
            _checkpoint("contact", binding_subject=5, binding_offset=1),
            _checkpoint("contact", processed=1, binding_subject=5, binding_offset=1),
            delta,
        )
    with pytest.raises(ValueError, match="final contact cursor"):
        build_company_membership_commit(
            contact_envelope(),
            mutation,
            expected,
            _checkpoint(
                "contact",
                cursor=6,
                processed=1,
                binding_subject=5,
                binding_offset=1,
            ),
            delta,
        )


def test_contact_position_cannot_clear_regress_or_skip_subject() -> None:
    mutation = _membership_mutation(_snapshot())
    expected = _checkpoint("contact", binding_subject=5, binding_offset=0)
    delta = StandaloneCrmUnitAccountingDelta(1, 0, 0)

    for subject, offset in ((None, None), (5, -1), (6, 0)):
        with pytest.raises(ValueError):
            build_company_membership_commit(
                contact_envelope(),
                mutation,
                expected,
                _checkpoint(
                    "contact",
                    processed=1,
                    binding_subject=subject,
                    binding_offset=offset,
                ),
                delta,
            )


def test_nonzero_failed_rows_are_rejected_for_both_mutation_families() -> None:
    membership = _membership_mutation(_snapshot())
    checkpoint = _checkpoint("contact", binding_subject=5, binding_offset=0)
    with pytest.raises(ValueError, match="failed_rows"):
        build_company_membership_commit(
            contact_envelope(),
            membership,
            checkpoint,
            checkpoint,
            StandaloneCrmUnitAccountingDelta(0, 0, 1),
        )

    observation = company_description()
    head = CrmCompanyDescriptionHead(observation.company_reference, observation)
    description = CrmCompanyDescriptionMutation(
        observation,
        CrmCompanyDescriptionHeadCompareAndSet(None, head),
    )
    company_checkpoint = _checkpoint("company")
    with pytest.raises(ValueError, match="failed_rows"):
        build_company_description_commit(
            company_envelope(),
            description,
            company_checkpoint,
            company_checkpoint,
            StandaloneCrmUnitAccountingDelta(0, 0, 1),
        )


def test_company_description_requires_parent_clock_scope_and_bound() -> None:
    observation = company_description()
    head = CrmCompanyDescriptionHead(observation.company_reference, observation)
    mutation = CrmCompanyDescriptionMutation(
        observation,
        CrmCompanyDescriptionHeadCompareAndSet(None, head),
    )
    expected = _checkpoint("company")
    proposed = _checkpoint("company", cursor=6, processed=1)

    with pytest.raises(ValueError, match="frozen upper bound"):
        build_company_description_commit(
            company_envelope(),
            mutation,
            expected,
            proposed,
            StandaloneCrmUnitAccountingDelta(1, 0, 0),
        )


def test_reference_helper_rejects_company_absent_from_complete_snapshot() -> None:
    with pytest.raises(ValueError, match="absent"):
        membership_company_reference(_snapshot(), "303")
