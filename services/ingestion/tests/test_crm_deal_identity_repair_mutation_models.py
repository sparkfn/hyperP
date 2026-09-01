"""Pure contracts for the #309 atomic mutation command and digest bindings."""

from __future__ import annotations

from typing import cast

import pytest
from neo4j import ManagedTransaction
from src.crm_deal_identity_repair.execution_models import RepairFence, RepairUnit
from src.crm_deal_identity_repair.models import RepairInventoryItem, RepairPartition
from src.crm_deal_identity_repair.mutation_models import (
    ProvenanceClass,
    RepairAuthorityEvidence,
    RepairMutationCommand,
    RepairMutationPlan,
    build_inventory_binding_digest,
    external_authority_evidence_digest,
)
from src.graph.crm_deal_identity_repair_mutation_authority import _authority_evidence

DIGEST = "sha256:" + "a" * 64


def _inventory(partition: RepairPartition = "ownership_repair") -> RepairInventoryItem:
    return RepairInventoryItem(
        source_system="bitrix_chat",
        source_record_id="bitrix-crm-deal-1",
        source_record_pk="deal-pk",
        deal_id="1",
        partition=partition,
        graph_fingerprint=DIGEST,
        stored_payload_fingerprint=DIGEST,
        payload={},
    )


def _unit(inventory: RepairInventoryItem) -> RepairUnit:
    return RepairUnit(
        "run",
        "unit",
        1,
        0,
        1,
        DIGEST,
        inventory.graph_fingerprint,
        "allocated",
        inventory.inventory_key,
        inventory.source_record_pk,
        inventory.graph_fingerprint,
        inventory.stored_payload_fingerprint,
        build_inventory_binding_digest(inventory),
    )


def _command() -> RepairMutationCommand:
    inventory = _inventory()
    return RepairMutationCommand(
        _unit(inventory),
        RepairFence("run", "unit", "fence", 1, 0, 1, "owner", "token", DIGEST, DIGEST, "claimed"),
        inventory,
        "source-instance",
        "control-instance",
    )


def test_mutation_command_derives_stable_ledger_identities_from_all_bound_evidence() -> None:
    command = _command()

    assert command.mutation_id == _command().mutation_id
    assert command.rollback_image_id != command.checkpoint_id
    assert command.checkpoint_id != command.outbox_event_id
    assert command.request_digest.startswith("sha256:")


def test_negative_controls_and_lost_fences_are_rejected_before_transaction_work() -> None:
    inventory = _inventory()
    unit = _unit(inventory)
    fence = RepairFence("run", "unit", "fence", 1, 0, 1, "owner", "token", DIGEST, DIGEST, "lost")
    with pytest.raises(ValueError, match="claimed fence"):
        RepairMutationCommand(unit, fence, inventory, "source-instance", "control-instance")
    with pytest.raises(ValueError, match="negative-control"):
        RepairMutationCommand(
            unit,
            RepairFence(
                "run", "unit", "fence", 1, 0, 1, "owner", "token", DIGEST, DIGEST, "claimed"
            ),
            _inventory("negative_control"),
            "source-instance",
            "control-instance",
        )


def test_external_authority_digest_excludes_repair_owned_review_rows_only() -> None:
    external = RepairAuthorityEvidence(
        "person-a",
        "reviewed_v2",
        ("external-reviewed",),
        (
            {
                "source_record_pk": "external-reviewed",
                "match_decision_id": "external-decision",
                "review_case_id": "external-review",
            },
        ),
    )
    repair_owned = RepairAuthorityEvidence(
        "person-a",
        "reviewed_v2",
        ("deal-pk",),
        (
            {
                "source_record_pk": "deal-pk",
                "decision_repair_mutation_id": "mutation-a",
            },
        ),
    )
    baseline = external_authority_evidence_digest(
        ("person-a",),
        (external,),
        mutation_id="mutation-a",
        excluded_source_record_pks=("deal-pk", "replacement-pk"),
    )
    assert (
        external_authority_evidence_digest(
            ("person-a",),
            (external, repair_owned),
            mutation_id="mutation-a",
            excluded_source_record_pks=("deal-pk", "replacement-pk"),
        )
        == baseline
    )


@pytest.mark.parametrize(
    "provenance",
    ["historical_deal_only", "self_supporting"],
)
def test_external_authority_digest_keeps_unrelated_disqualifying_evidence(
    provenance: ProvenanceClass,
) -> None:
    baseline = external_authority_evidence_digest(
        ("person-a",), (), mutation_id="mutation-a", excluded_source_record_pks=("deal-pk",)
    )
    unrelated = RepairAuthorityEvidence(
        "person-a",
        provenance,
        ("unrelated-pk",),
        ({"source_record_pk": "unrelated-pk", "source_record_id": "unrelated"},),
    )
    assert (
        external_authority_evidence_digest(
            ("person-a",),
            (unrelated,),
            mutation_id="mutation-a",
            excluded_source_record_pks=("deal-pk",),
        )
        != baseline
    )


class _AuthorityRowsTransaction:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def run(self, query: str, **parameters: object) -> list[dict[str, object]]:
        del query, parameters
        return self._rows


def test_authority_evidence_rows_and_persons_are_canonically_sorted() -> None:
    row_template: dict[str, object] = {
        "reviewed_rows": [],
        "historical_rows": [],
        "self_rows": [],
        "active_no_match_locks": 0,
    }
    rows = [
        {
            **row_template,
            "person_id": "person-b",
            "independent_rows": [
                {"source_record_pk": "support-z", "source_entity_id": "z"},
                {"source_record_pk": "support-a", "source_entity_id": "a"},
            ],
        },
        {
            **row_template,
            "person_id": "person-a",
            "independent_rows": [{"source_record_pk": "support-b", "source_entity_id": "b"}],
        },
    ]
    evidence = _authority_evidence(
        cast(ManagedTransaction, _AuthorityRowsTransaction(rows)),
        _command(),
        ("person-b", "person-a"),
    )
    assert [(item.person_id, item.provenance_class) for item in evidence] == [
        ("person-a", "blocked_or_conflicting"),
        ("person-a", "independent_trusted"),
        ("person-b", "blocked_or_conflicting"),
        ("person-b", "independent_trusted"),
    ]
    assert [row["source_record_pk"] for row in evidence[3].evidence_rows] == [
        "support-a",
        "support-z",
    ]


def test_external_authority_digest_is_independent_of_evidence_and_row_order() -> None:
    first = RepairAuthorityEvidence(
        "person-b",
        "historical_deal_only",
        ("historical-b",),
        ({"source_record_pk": "historical-b", "source_record_id": "b"},),
    )
    second = RepairAuthorityEvidence(
        "person-a",
        "self_supporting",
        ("child-z", "child-a"),
        (
            {"source_record_pk": "child-z"},
            {"source_record_pk": "child-a"},
        ),
    )
    expected = external_authority_evidence_digest(
        ("person-a", "person-b"),
        (first, second),
        mutation_id=None,
        excluded_source_record_pks=(),
    )
    actual = external_authority_evidence_digest(
        ("person-b", "person-a", "person-a"),
        (
            RepairAuthorityEvidence(
                "person-a",
                "self_supporting",
                ("child-a", "child-z"),
                (
                    {"source_record_pk": "child-a"},
                    {"source_record_pk": "child-z"},
                ),
            ),
            first,
        ),
        mutation_id=None,
        excluded_source_record_pks=(),
    )
    assert actual == expected
    plan = RepairMutationPlan(
        disposition="applied",
        source_record_payload={},
        source_record_pk="replacement-pk",
        source_record_version=2,
        selected_person_id="person-a",
        provisional_person_id=None,
        current_owner_ids=("person-a", "person-b"),
        authority_evidence=(first, second),
        reason_codes=("test",),
        retired_source_record_pks=("deal-pk",),
    )
    reordered_plan = RepairMutationPlan(
        disposition="applied",
        source_record_payload={},
        source_record_pk="replacement-pk",
        source_record_version=2,
        selected_person_id="person-a",
        provisional_person_id=None,
        current_owner_ids=("person-b", "person-a", "person-a"),
        authority_evidence=(second, first),
        reason_codes=("test",),
        retired_source_record_pks=("deal-pk",),
    )
    assert reordered_plan.authority_digest == plan.authority_digest
    assert reordered_plan.external_authority_digest == plan.external_authority_digest


def test_external_authority_digest_keeps_lock_only_blockers_stable_and_sensitive() -> None:
    baseline = external_authority_evidence_digest(
        ("person-a",),
        (
            RepairAuthorityEvidence(
                "person-a",
                "blocked_or_conflicting",
                (),
                (
                    {
                        "active_no_match_lock_count": 1,
                        "current_owner_count": 1,
                        "source_record_pk": "deal-pk",
                    },
                ),
            ),
        ),
        mutation_id="mutation-a",
        excluded_source_record_pks=("deal-pk", "replacement-pk"),
    )
    assert baseline == external_authority_evidence_digest(
        ("person-a",),
        (
            RepairAuthorityEvidence(
                "person-a",
                "blocked_or_conflicting",
                (),
                (
                    {
                        "active_no_match_lock_count": 1,
                        "current_owner_count": 1,
                        "source_record_pk": "replacement-pk",
                        "decision_repair_mutation_id": "mutation-a",
                    },
                ),
            ),
        ),
        mutation_id="mutation-a",
        excluded_source_record_pks=("deal-pk", "replacement-pk"),
    )
    assert baseline != external_authority_evidence_digest(
        ("person-a",),
        (
            RepairAuthorityEvidence(
                "person-a",
                "blocked_or_conflicting",
                (),
                ({"active_no_match_lock_count": 0, "current_owner_count": 1},),
            ),
        ),
        mutation_id="mutation-a",
        excluded_source_record_pks=("deal-pk", "replacement-pk"),
    )
