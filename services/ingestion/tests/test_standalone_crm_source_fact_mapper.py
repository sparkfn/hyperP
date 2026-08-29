from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_openlines.models import CrmContact
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_source_fact_mapper import map_source_fact_page
from src.standalone_crm_source_fact_models import (
    MAX_STANDALONE_CRM_SOURCE_FACT_PAGE_ROWS,
    StandaloneCrmSourceFactPage,
)
from tests._standalone_crm_lane_a_fakes import contact_envelope


def _page(rows: tuple[CrmContact, ...]) -> StandaloneCrmSourceFactPage:
    envelope = replace(contact_envelope(), binding_subposition=None)
    checkpoint = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, None, None, 0, 0, 1, 2)
    return StandaloneCrmSourceFactPage(envelope, "call-a", 5, checkpoint, rows)


def test_mapper_uses_parent_clock_not_retry_clock_and_excludes_associations() -> None:
    row = CrmContact("6", "Ada", ("+6512345678",), (), "contact", datetime(2020, 1, 1, tzinfo=UTC))
    first = map_source_fact_page(_page((row,))).mapped_rows[0].envelope
    second = map_source_fact_page(_page((row,))).mapped_rows[0].envelope
    assert first.record_hash == second.record_hash
    assert first.raw_payload["standalone_crm_source_fact"]["available_at"] == "2026-08-28T00:00:00Z"
    assert first.source_entity_type == "contact"
    assert first.identity_policy_version == "crm_contact_identity_v1"
    assert "company_id" not in first.raw_payload


def test_authorized_invalid_content_is_failed_but_bad_order_rejects_page() -> None:
    malformed = CrmContact("6", "", (), (), "contact")
    mutation = map_source_fact_page(_page((malformed,)))
    assert mutation.mapped_rows == ()
    assert mutation.failed_rows == 1
    with pytest.raises(ValueError, match="strictly increasing"):
        _page((CrmContact("6", None, kind="contact"), CrmContact("6", None, kind="contact")))
    with pytest.raises(ValueError, match="frozen"):
        _page((CrmContact("11", None, kind="contact"),))
    association = map_source_fact_page(
        _page((CrmContact("6", None, kind="contact", company_id="9"),))
    )
    assert association.failed_rows == 1


def test_contact_and_lead_match_the_existing_policy_and_suppress_oversized_channels() -> None:
    from src.connectors.bitrix_openlines.crm_identity_policy import (
        crm_standalone_contact_identity_evidence,
        crm_standalone_lead_identity_evidence,
    )
    from tests._standalone_crm_lane_a_fakes import lead_envelope

    contact = CrmContact(
        "6", "Ada", tuple(f"+6500{i}" for i in range(6)), ("a@example.test",), "contact"
    )
    contact_mapped = map_source_fact_page(_page((contact,))).mapped_rows[0].envelope
    contact_policy = crm_standalone_contact_identity_evidence(
        contact, source_instance_id="portal-a"
    )
    assert [
        {"type": item.type, "value": item.value, "is_verified": item.is_verified}
        for item in contact_mapped.identifiers
    ] == list(contact_policy.identifiers)
    assert (
        contact_mapped.identity_policy_version == contact_policy.metadata["identity_policy_version"]
    )
    assert [item.type for item in contact_mapped.identifiers] == ["crm_contact_id"]

    lead = CrmContact("6", "Ada", (), ("a@example.test",), "lead")
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, None, None, 0, 0, 1, 2)
    lead_mapped = (
        map_source_fact_page(
            StandaloneCrmSourceFactPage(lead_envelope(), "call-a", 5, checkpoint, (lead,))
        )
        .mapped_rows[0]
        .envelope
    )
    lead_policy = crm_standalone_lead_identity_evidence(lead, source_instance_id="portal-a")
    assert [
        {"type": item.type, "value": item.value, "is_verified": item.is_verified}
        for item in lead_mapped.identifiers
    ] == list(lead_policy.identifiers)
    assert lead_mapped.identity_policy_version == lead_policy.metadata["identity_policy_version"]
    assert lead_mapped.source_record_id == "bitrix-crm-lead-6"


def test_hash_is_stable_across_operational_authority_but_isolates_source_instances() -> None:
    from src.standalone_crm_child_contracts import StandaloneCrmSourceChildScope

    row = CrmContact("6", "Ada", kind="contact")
    base = _page((row,)).envelope
    changed_authority = replace(
        base,
        unit=replace(base.unit, generation=2, fence_token=3, task_id="other-task"),
        budget_authorization=replace(
            base.budget_authorization,
            generation=2,
            fence_token=3,
            task_id="other-task",
        ),
    )
    changed_checkpoint = StandaloneCrmCheckpoint(
        "census-a", "contact", 10, None, 5, None, None, 0, 0, 2, 3
    )
    changed = StandaloneCrmSourceFactPage(
        changed_authority, "call-a", 5, changed_checkpoint, (row,)
    )
    isolated = replace(
        base, scope=StandaloneCrmSourceChildScope("bitrix_chat", "portal-b", "control-a")
    )
    isolated_page = StandaloneCrmSourceFactPage(
        isolated,
        "call-a",
        5,
        StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, None, None, 0, 0, 1, 2),
        (row,),
    )
    assert (
        map_source_fact_page(_page((row,))).mapped_rows[0].envelope.record_hash
        == map_source_fact_page(changed).mapped_rows[0].envelope.record_hash
    )
    assert (
        map_source_fact_page(_page((row,))).mapped_rows[0].envelope.record_hash
        != map_source_fact_page(isolated_page).mapped_rows[0].envelope.record_hash
    )


def test_timestampless_rows_use_parent_availability_without_wall_clock_or_hash_churn() -> None:
    row = CrmContact("6", "Ada", kind="contact")
    first = map_source_fact_page(_page((row,))).mapped_rows[0].envelope
    second = map_source_fact_page(_page((row,))).mapped_rows[0].envelope

    assert first.observed_at == "2026-08-28T00:00:00Z"
    assert first.raw_payload["observed_at"] is None
    assert first.raw_payload["effective_observed_at"] == "2026-08-28T00:00:00Z"
    assert first.record_hash == second.record_hash


def test_page_boundary_accepts_fifty_rows_and_rejects_fifty_one_before_mapping() -> None:
    accepted = tuple(
        CrmContact(str(identifier), "Ada", kind="contact")
        for identifier in range(6, 6 + MAX_STANDALONE_CRM_SOURCE_FACT_PAGE_ROWS)
    )
    envelope = replace(
        _page((CrmContact("6", "Ada", kind="contact"),)).envelope, frozen_upper_id=100
    )
    checkpoint = StandaloneCrmCheckpoint(
        "census-a", "contact", 100, None, 5, None, None, 0, 0, 1, 2
    )
    assert len(StandaloneCrmSourceFactPage(envelope, "call-a", 5, checkpoint, accepted).rows) == 50
    rejected = accepted + (CrmContact("56", "Ada", kind="contact"),)
    with pytest.raises(ValueError, match="50-row"):
        StandaloneCrmSourceFactPage(envelope, "call-a", 5, checkpoint, rejected)
