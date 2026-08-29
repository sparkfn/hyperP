"""Provenance and deterministic reconstruction tests for the #309 repair seam."""

from __future__ import annotations

from datetime import UTC, datetime

from src.connectors.bitrix_openlines.connector import build_crm_deal_envelope
from src.connectors.bitrix_openlines.models import CrmContact, CrmDeal
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.crm_deal_identity_repair.mutation_classifier import (
    ParsedRepairInventory,
    build_repair_plan,
    parse_repair_inventory,
)
from src.crm_deal_identity_repair.mutation_models import RepairAuthorityEvidence
from src.models import MatchDecision, MatchResult, RawIdentifier, RecordType, SourceRecordEnvelope

DIGEST = "sha256:" + "b" * 64


def _parsed() -> ParsedRepairInventory:
    item = RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-1",
        source_record_pk="contaminated-deal",
        deal_id="1",
        partition="ownership_repair",
        graph_fingerprint=DIGEST,
        stored_payload_fingerprint=DIGEST,
        payload={},
    )
    return ParsedRepairInventory(
        item=item,
        envelope=SourceRecordEnvelope(
            source_system="bitrix_chat",
            source_instance_id="portal-a",
            source_record_id="bitrix-crm-deal-1",
            source_record_version="2",
            entity_key="tenant-a",
            record_type=RecordType.CRM_DEAL,
            observed_at="2026-08-01T12:00:00+00:00",
            record_hash="b" * 64,
            identifiers=[
                RawIdentifier(
                    type="crm_contact_id", value="contact-1", source_instance_id="portal-a"
                )
            ],
            raw_payload={"crm_deal_identity_policy_version": "crm_deal_identity_v2"},
            source_entity_type="deal",
            source_entity_id="1",
            identity_policy_version="crm_deal_identity_v2",
            identity_link_key="bitrix:portal-a:deal:1",
        ),
        source_record_version=2,
        current_owner_ids=("person-a",),
        descendant_source_record_pks=(),
        reconstructable=True,
    )


def _match() -> MatchResult:
    return MatchResult(
        decision=MatchDecision.MERGE,
        confidence=1.0,
        matched_person_id="person-a",
        reasons=["canonical_crm_contact_id"],
    )


def _inventory_payload(*, observed_at: str | None = "2026-08-01T12:00:00+00:00") -> dict:
    contact = CrmContact("contact-1", None, phones=("+6591234567",), kind="contact")
    built = build_crm_deal_envelope(
        CrmDeal(
            id="1",
            title="Deal 1",
            category_id="1",
            stage_id="NEW",
            observed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
            primary_contact=contact,
            contacts=(contact,),
            contact_count=1,
            has_ambiguous_contacts=False,
            raw_payload={"ID": "1"},
        ),
        "tenant-a",
        source_instance_id="portal-a",
    )
    return {
        "source_record_version": "1",
        "lifecycle_status": "active",
        "is_latest": True,
        "record_hash": built["record_hash"],
        "observed_at": observed_at,
        "raw_payload": built["raw_payload"],
        "normalized_payload": {},
        "linked_people": [{"person_id": "person-a", "is_active": True}],
        "projections": [],
        "logical_version_evidence": {},
        "lifecycle_policy_evidence": {},
        "descendants": [],
        "decisions_and_reviews": [],
        "owner_impacts": [],
    }


def _inventory(*, observed_at: str | None = "2026-08-01T12:00:00+00:00") -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-1",
        source_record_pk="contaminated-deal",
        deal_id="1",
        partition="projection_cleanup",
        graph_fingerprint=DIGEST,
        stored_payload_fingerprint=DIGEST,
        payload=_inventory_payload(observed_at=observed_at),
    )


def test_reconstruction_uses_locked_scope_entity_and_frozen_time_exactly() -> None:
    parsed = parse_repair_inventory(_inventory(), "portal-a", "tenant-a")
    assert parsed.envelope is not None
    assert parsed.envelope.source_instance_id == "portal-a"
    assert parsed.envelope.entity_key == "tenant-a"
    assert parsed.envelope.observed_at == "2026-08-01T12:00:00+00:00"
    assert parsed.envelope.identity_link_key == "bitrix:portal-a:deal:1"
    assert parsed.envelope.record_hash == _inventory_payload()["record_hash"]
    scoped = [item for item in parsed.envelope.identifiers if item.type == "crm_contact_id"]
    assert {item.source_instance_id for item in scoped} == {"portal-a"}


def test_reconstruction_changes_only_scope_bound_identity_for_another_portal() -> None:
    parsed = parse_repair_inventory(_inventory(), "portal-b", "tenant-a")
    assert parsed.envelope is not None
    assert parsed.envelope.identity_link_key == "bitrix:portal-b:deal:1"
    scoped = [item for item in parsed.envelope.identifiers if item.type == "crm_contact_id"]
    assert {item.source_instance_id for item in scoped} == {"portal-b"}


def test_missing_or_naive_frozen_time_fails_closed_to_review() -> None:
    for observed_at in (None, "2026-08-01T12:00:00"):
        parsed = parse_repair_inventory(_inventory(observed_at=observed_at), "portal-a", "tenant-a")
        assert parsed.envelope is None
        plan = build_repair_plan(parsed, _match(), ())
        assert plan.disposition == "review_required"
        assert plan.source_record_payload is None
        assert plan.reason_codes == ("unreconstructable_v2_payload",)


def test_historical_or_self_supporting_evidence_never_selects_an_automatic_owner() -> None:
    parsed = _parsed()
    for provenance_class, source_pks in (
        ("historical_deal_only", ("historical-deal",)),
        ("self_supporting", ("contaminated-deal",)),
    ):
        plan = build_repair_plan(
            parsed,
            _match(),
            (RepairAuthorityEvidence("person-a", provenance_class, source_pks, ()),),
        )
        assert plan.disposition == "review_required"
        assert plan.selected_person_id is None
        assert plan.provisional_person_id == "person-a"


def test_independent_or_reviewed_v2_evidence_can_preserve_the_exact_owner() -> None:
    for provenance_class in ("independent_trusted", "reviewed_v2"):
        plan = build_repair_plan(
            _parsed(),
            _match(),
            (
                RepairAuthorityEvidence(
                    "person-a",
                    provenance_class,
                    ("independent-version",),
                    ({"source_record_pk": "independent-version"},),
                ),
            ),
        )
        assert plan.disposition == "applied"
        assert plan.selected_person_id == "person-a"
        assert plan.provisional_person_id is None
