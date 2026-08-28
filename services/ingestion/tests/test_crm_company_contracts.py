"""Contract tests for immutable CRM company facts and complete memberships."""

from __future__ import annotations

import hashlib
import json
from typing import cast

import pytest
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import (
    CrmCompanyDescriptionHead,
    CrmCompanyDescriptionHeadCompareAndSet,
    CrmCompanyDescriptionObservation,
    CrmCompanyMembershipHead,
    CrmCompanyMembershipHeadCompareAndSet,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
    CrmCompanyReference,
    CrmSourceHeadOrderKey,
)
from src.crm_identity_associations import (
    CrmCompanyMembershipSnapshot,
    normalize_company_membership_snapshot,
)
from src.standalone_crm_child_contracts import (
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildScope,
)

_AVAILABLE_AT = "2026-08-28T01:02:03Z"
_OBSERVED_AT = "2026-08-27T01:02:03+00:00"


def _scope() -> StandaloneCrmSourceChildScope:
    return StandaloneCrmSourceChildScope(
        source_key="bitrix_chat",
        source_instance_id="bitrix-primary",
        control_instance_id="bitrix-control",
    )


def _availability() -> StandaloneCrmSourceAvailability:
    return StandaloneCrmSourceAvailability(available_at=_AVAILABLE_AT)


def _reference(company_id: str = "303") -> CrmCompanyReference:
    return CrmCompanyReference(
        scope=_scope(),
        company_id=company_id,
        source_record_id=f"bitrix-crm-company-{company_id}",
    )


def _description(
    description: str | None = "Northwind",
    version: int = 1,
    pk: str = "company-record-1",
) -> CrmCompanyDescriptionObservation:
    return CrmCompanyDescriptionObservation(
        company_reference=_reference(),
        source_record_pk=pk,
        source_record_version=version,
        source_record_hash="company-hash",
        description=description,
        observed_at=_OBSERVED_AT,
        availability=_availability(),
    )


def _snapshot_record(
    bindings: tuple[CrmCompanyBindingPayload, ...] = (),
) -> CrmCompanyMembershipSnapshotRecord:
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact", subject_id="101", payloads=bindings
    )
    return CrmCompanyMembershipSnapshotRecord(
        scope=_scope(),
        membership_snapshot=snapshot,
        source_record_id="bitrix-crm-contact-101",
        source_record_pk="contact-record-1",
        source_record_version=1,
        source_record_hash="contact-hash",
        observed_at=_OBSERVED_AT,
        availability=_availability(),
        binding_count=len(snapshot.bindings),
    )


def test_company_reference_is_immutable_scoped_and_person_prohibited() -> None:
    reference = _reference()

    assert reference.identity_policy_version == "crm_company_reference_v1"
    assert reference.person_matching_prohibited is True
    with pytest.raises(ValueError, match="canonical positive decimal"):
        CrmCompanyReference(_scope(), "0303", "bitrix-crm-company-303")
    with pytest.raises(ValueError, match="prohibit Person matching"):
        CrmCompanyReference(
            _scope(),
            "303",
            "bitrix-crm-company-303",
            person_matching_prohibited=False,
        )
    assert reference == _reference()


def test_source_head_ordering_uses_instants_before_version_and_primary_key() -> None:
    earlier = CrmSourceHeadOrderKey("2026-08-28T00:00:00Z", 9, "z")
    later = CrmSourceHeadOrderKey("2026-08-28T00:00:00.100000Z", 1, "a")

    assert later > earlier
    assert CrmSourceHeadOrderKey("2026-08-28T00:00:00Z", 2, "a") > (
        CrmSourceHeadOrderKey("2026-08-28T00:00:00Z", 1, "z")
    )


def test_company_contracts_reject_malformed_nested_boundaries() -> None:
    record = _snapshot_record((CrmCompanyBindingPayload("303", 4, "7", "Y"),))
    bad_scope = cast(StandaloneCrmSourceChildScope, "invalid")
    bad_reference = cast(CrmCompanyReference, "invalid")
    bad_availability = cast(StandaloneCrmSourceAvailability, "invalid")
    bad_snapshot = cast(CrmCompanyMembershipSnapshot, "invalid")
    bad_record = cast(CrmCompanyMembershipSnapshotRecord, "invalid")
    bad_observation = cast(CrmCompanyDescriptionObservation, "invalid")
    bad_description_head = cast(CrmCompanyDescriptionHead, "invalid")
    bad_membership_head = cast(CrmCompanyMembershipHead, "invalid")

    with pytest.raises(ValueError, match="source scope"):
        CrmCompanyReference(bad_scope, "303", "bitrix-crm-company-303")
    with pytest.raises(ValueError, match="company reference"):
        CrmCompanyDescriptionObservation(
            bad_reference, "company-record-1", 1, "company-hash", None, None, _availability()
        )
    with pytest.raises(ValueError, match="source availability"):
        CrmCompanyDescriptionObservation(
            _reference(), "company-record-1", 1, "company-hash", None, None, bad_availability
        )
    with pytest.raises(ValueError, match="membership snapshot"):
        CrmCompanyMembershipSnapshotRecord(
            _scope(),
            bad_snapshot,
            "bitrix-crm-contact-101",
            "contact-record-1",
            1,
            "contact-hash",
            None,
            _availability(),
            0,
        )
    with pytest.raises(ValueError, match="source scope"):
        CrmCompanyMembershipSnapshotRecord(
            bad_scope,
            record.membership_snapshot,
            "bitrix-crm-contact-101",
            "contact-record-1",
            1,
            "contact-hash",
            None,
            _availability(),
            1,
        )
    with pytest.raises(ValueError, match="source availability"):
        CrmCompanyMembershipSnapshotRecord(
            _scope(),
            record.membership_snapshot,
            "bitrix-crm-contact-101",
            "contact-record-1",
            1,
            "contact-hash",
            None,
            bad_availability,
            1,
        )
    with pytest.raises(ValueError, match="description observation"):
        CrmCompanyDescriptionHead(_reference(), bad_observation)
    with pytest.raises(ValueError, match="snapshot record"):
        CrmCompanyMembershipObservation(bad_record, _reference(), 4, "7", True)
    with pytest.raises(ValueError, match="company reference"):
        CrmCompanyMembershipObservation(record, bad_reference, 4, "7", True)
    with pytest.raises(ValueError, match="snapshot record"):
        CrmCompanyMembershipHead(_scope(), "contact", "101", bad_record)
    with pytest.raises(ValueError, match="description head"):
        CrmCompanyDescriptionHeadCompareAndSet(None, bad_description_head)
    with pytest.raises(ValueError, match="membership head"):
        CrmCompanyMembershipHeadCompareAndSet(None, bad_membership_head)


@pytest.mark.parametrize("description", [None, "", "Northwind"])
def test_description_preserves_null_empty_and_nonempty_title(description: str | None) -> None:
    observation = _description(description)

    assert observation.description is description
    assert observation.company_title is description
    assert observation.observed_at == "2026-08-27T01:02:03Z"
    assert observation.availability.available_at == _AVAILABLE_AT
    assert len(observation.observation_id) == 64


def test_description_digest_is_deterministic_and_uses_a_nul_domain_separator() -> None:
    observation = _description()
    normalized_observed_at = observation.observed_at
    assert normalized_observed_at == "2026-08-27T01:02:03Z"
    payload = [
        "bitrix-primary",
        "bitrix-control",
        "303",
        "bitrix-crm-company-303",
        "company-record-1",
        1,
        "company-hash",
        "Northwind",
        normalized_observed_at,
        _AVAILABLE_AT,
        "crm-company-description-v1",
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    expected = hashlib.sha256(
        b"crm-company-description-observation-v1\x00" + encoded.encode("utf-8")
    ).hexdigest()

    assert observation.observation_id == expected
    assert observation.observation_id == _description().observation_id


def test_description_head_cas_is_ordered_forward_only_and_idempotent() -> None:
    older = CrmCompanyDescriptionHead(_reference(), _description(version=1, pk="company-record-1"))
    newer = CrmCompanyDescriptionHead(_reference(), _description(version=2, pk="company-record-2"))
    conflicting = CrmCompanyDescriptionHead(
        _reference(), _description("Different", version=1, pk="company-record-1")
    )

    assert CrmCompanyDescriptionHeadCompareAndSet(None, older).decision_for(None) == "advance"
    assert CrmCompanyDescriptionHeadCompareAndSet(older, newer).decision_for(older) == "advance"
    assert CrmCompanyDescriptionHeadCompareAndSet(None, older).decision_for(older) == "idempotent"
    assert (
        CrmCompanyDescriptionHeadCompareAndSet(older, conflicting).decision_for(older)
        == "stale_or_conflict"
    )
    assert newer.order_key.available_at == _AVAILABLE_AT
    assert newer.order_key.source_record_version == 2
    assert newer.order_key.source_record_pk == "company-record-2"


def test_empty_snapshot_is_complete_authoritative_membership_evidence() -> None:
    record = _snapshot_record()
    head = CrmCompanyMembershipHead(_scope(), "contact", "101", record)

    assert record.membership_snapshot.bindings == ()
    assert record.binding_count == 0
    assert record.snapshot_id != record.membership_snapshot.digest
    assert len(record.snapshot_id) == 64
    assert record.complete_set_digest == record.membership_snapshot.digest
    assert record.snapshot_digest == record.membership_snapshot.digest
    assert record.subject_kind == "contact"
    assert CrmCompanyMembershipHeadCompareAndSet(None, head).decision_for(None) == "advance"
    with pytest.raises(ValueError, match="binding_count"):
        CrmCompanyMembershipSnapshotRecord(
            scope=_scope(),
            membership_snapshot=record.membership_snapshot,
            source_record_id="bitrix-crm-contact-101",
            source_record_pk="contact-record-1",
            source_record_version=1,
            source_record_hash="contact-hash",
            observed_at=_OBSERVED_AT,
            availability=_availability(),
            binding_count=1,
        )


def test_membership_observation_requires_exact_normalized_binding() -> None:
    record = _snapshot_record((CrmCompanyBindingPayload("303", 4, "7", "Y"),))
    observation = CrmCompanyMembershipObservation(record, _reference(), 4, "7", True)

    assert observation.snapshot_id == record.snapshot_id
    assert observation.company_id == "303"
    assert observation.subject_type == "contact"
    assert observation.subject_kind == "contact"
    assert len(observation.observation_id) == 64
    with pytest.raises(ValueError, match="exactly match"):
        CrmCompanyMembershipObservation(record, _reference(), 5, "7", True)
    with pytest.raises(ValueError, match="exist in the complete snapshot"):
        CrmCompanyMembershipObservation(record, _reference("404"), 4, "7", True)


def test_membership_head_cas_is_forward_only_and_exact_replay_is_idempotent() -> None:
    current_record = _snapshot_record()
    current = CrmCompanyMembershipHead(_scope(), "contact", "101", current_record)
    proposed_record = CrmCompanyMembershipSnapshotRecord(
        scope=_scope(),
        membership_snapshot=current_record.membership_snapshot,
        source_record_id="bitrix-crm-contact-101",
        source_record_pk="contact-record-2",
        source_record_version=2,
        source_record_hash="contact-hash-2",
        observed_at=_OBSERVED_AT,
        availability=_availability(),
        binding_count=0,
    )
    proposed = CrmCompanyMembershipHead(_scope(), "contact", "101", proposed_record)

    assert (
        CrmCompanyMembershipHeadCompareAndSet(current, proposed).decision_for(current) == "advance"
    )
    assert (
        CrmCompanyMembershipHeadCompareAndSet(None, current).decision_for(current) == "idempotent"
    )
    assert (
        CrmCompanyMembershipHeadCompareAndSet(proposed, current).decision_for(current)
        == "stale_or_conflict"
    )
    assert current.subject_kind == "contact"
