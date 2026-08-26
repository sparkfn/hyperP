"""Unit tests for complete standalone CRM company-membership source facts."""

from __future__ import annotations

import pytest
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_identity_associations import (
    lead_membership_snapshot,
    normalize_company_membership_snapshot,
)


def _binding(
    company_id: object,
    *,
    sort: object = None,
    role_id: object = None,
    is_primary: object = False,
) -> CrmCompanyBindingPayload:
    return CrmCompanyBindingPayload(company_id, sort, role_id, is_primary)


def test_complete_contact_snapshot_is_canonical_and_duplicate_safe() -> None:
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="00101",
        payloads=(
            _binding("20", sort="5", role_id="7", is_primary="N"),
            _binding(10, sort=0, role_id=1, is_primary="Y"),
            _binding("20", sort=5, role_id=7, is_primary=0),
        ),
    )

    assert snapshot.subject_id == "101"
    assert [binding.company_id for binding in snapshot.bindings] == ["10", "20"]
    assert snapshot.bindings[0].is_primary is True
    assert snapshot.bindings[0].role_id == "1"
    assert len(snapshot.digest) == 64


def test_complete_empty_lead_snapshot_is_stable_negative_evidence() -> None:
    first = lead_membership_snapshot(lead_id="202", company_id=None)
    second = lead_membership_snapshot(lead_id="202", company_id="  ")

    assert first.bindings == ()
    assert first.digest == second.digest


@pytest.mark.parametrize(
    ("bindings", "message"),
    [
        ((_binding("1", is_primary="Y"), _binding("2", is_primary=1)), "more than one"),
        ((_binding("1", sort=1), _binding("1", sort=2)), "conflicting duplicate"),
        ((_binding("0"),), "positive decimal"),
        ((_binding("1", role_id=0),), "positive decimal"),
        ((_binding("1", is_primary="yes"),), "IS_PRIMARY"),
    ],
)
def test_invalid_or_ambiguous_contact_memberships_fail_closed(
    bindings: tuple[CrmCompanyBindingPayload, ...], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_company_membership_snapshot(
            subject_type="contact",
            subject_id="101",
            payloads=bindings,
        )


def test_unsupported_membership_contract_fails_closed() -> None:
    with pytest.raises(ValueError, match="contract_version is unsupported"):
        lead_membership_snapshot(
            lead_id="202",
            company_id="3",
            contract_version="crm-company-membership-snapshot-v2",
        )


def test_sort_boundaries_and_null_role_order_are_canonical() -> None:
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="101",
        payloads=(
            _binding("2", sort=2_147_483_647, role_id=None),
            _binding("1", sort=0, role_id=1),
        ),
    )
    assert [binding.company_id for binding in snapshot.bindings] == ["1", "2"]
    with pytest.raises(ValueError, match="32-bit"):
        normalize_company_membership_snapshot(
            subject_type="contact",
            subject_id="101",
            payloads=(_binding("1", sort=2_147_483_648),),
        )
