"""Pure secondary-closure accounting tests."""

from __future__ import annotations

import pytest
from src.graph.crm_deal_identity_repair_verification_secondary import (
    FrozenContextSubject,
    SecondarySubjectError,
    assert_current_context,
    expected_post_repair_context,
    frozen_context_subjects,
    frozen_pair_case_ids,
    override_entries,
)


def test_frozen_context_preserves_descendant_merge_and_lock_subjects() -> None:
    values = frozen_context_subjects(
        {
            "descendants": [{"source_record_pk": "child-a", "record_type": "crm_history"}],
            "owner_impacts": [
                {"evidence_type": "merge_lineage", "merge_event_id": "merge-a"},
                {"evidence_type": "no_match_lock", "no_match_lock_id": "lock-a"},
            ],
        }
    )
    assert [(value.kind, value.stable_id) for value in values] == [
        ("descendant", "child-a"),
        ("merge_lineage", "merge-a"),
        ("no_match_lock", "lock-a"),
    ]
    assert_current_context(
        values,
        values,
    )


@pytest.mark.parametrize(
    "current",
    (
        (),
        (
            FrozenContextSubject("descendant", "child-a", {"source_record_pk": "child-a"}),
            FrozenContextSubject("descendant", "child-a", {"source_record_pk": "child-a"}),
        ),
        (FrozenContextSubject("descendant", "unexpected", {}),),
    ),
)
def test_context_requires_exact_subject_set(current: tuple[FrozenContextSubject, ...]) -> None:
    expected = frozen_context_subjects({"descendants": [{"source_record_pk": "child-a"}]})
    with pytest.raises(SecondarySubjectError, match="closure differs"):
        assert_current_context(expected, current)


def test_context_requires_same_id_evidence_to_remain_exact() -> None:
    expected = frozen_context_subjects(
        {"descendants": [{"source_record_pk": "child-a", "lifecycle_status": "active"}]}
    )
    current = (
        FrozenContextSubject(
            "descendant",
            "child-a",
            {"source_record_pk": "child-a", "lifecycle_status": "superseded"},
        ),
    )
    with pytest.raises(SecondarySubjectError, match="closure differs"):
        assert_current_context(expected, current)


def test_active_descendant_link_is_compared_to_exact_retired_post_state() -> None:
    frozen = frozen_context_subjects(
        {
            "descendants": [
                {
                    "source_record_pk": "child-a",
                    "relationship_type": "LINKED_TO",
                    "relationship_is_active": True,
                    "owner_person_id": "person-a",
                }
            ]
        }
    )
    expected = expected_post_repair_context(frozen, "mutation-a")
    assert_current_context(
        expected,
        (
            FrozenContextSubject(
                "descendant",
                "child-a",
                {
                    "source_record_pk": "child-a",
                    "relationship_type": "LINKED_TO",
                    "relationship_is_active": False,
                    "retired_by_repair_mutation_id": "mutation-a",
                    "owner_person_id": "person-a",
                },
            ),
        ),
    )
    with pytest.raises(SecondarySubjectError, match="closure differs"):
        assert_current_context(
            expected,
            (
                FrozenContextSubject(
                    "descendant",
                    "child-a",
                    {
                        "source_record_pk": "child-a",
                        "relationship_type": "LINKED_TO",
                        "relationship_is_active": False,
                        "retired_by_repair_mutation_id": "other-mutation",
                        "owner_person_id": "person-a",
                    },
                ),
            ),
        )


def test_inactive_descendant_retains_unauthenticated_prior_retirement_stamp() -> None:
    frozen = frozen_context_subjects(
        {
            "descendants": [
                {
                    "source_record_pk": "child-a",
                    "relationship_type": "LINKED_TO",
                    "relationship_is_active": False,
                    "owner_person_id": "person-a",
                }
            ]
        }
    )
    assert_current_context(
        expected_post_repair_context(frozen, "mutation-a"),
        (
            FrozenContextSubject(
                "descendant",
                "child-a",
                {
                    "source_record_pk": "child-a",
                    "relationship_type": "LINKED_TO",
                    "relationship_is_active": False,
                    "retired_by_repair_mutation_id": "prior-mutation",
                    "owner_person_id": "person-a",
                },
            ),
        ),
    )


def test_override_entries_are_per_field_and_source_identity() -> None:
    entries = override_entries(
        "person-a",
        '{"preferred_email":{"source_record_pk":"source-a","source_kind":"identifier"},'
        '"preferred_phone":{"source_record_pk":"","custom_value":"+6500000000"}}',
    )
    assert [key for key, _ in entries] == [
        "person-a:preferred_email:source-a",
        "person-a:preferred_phone:custom",
    ]


def test_frozen_pair_cases_include_owner_impact_evidence_without_duplicates() -> None:
    assert frozen_pair_case_ids(
        {
            "decisions_and_reviews": [{"evidence_type": "pair_audit", "review_case_id": "pair-a"}],
            "owner_impacts": [{"evidence_type": "pair_audit", "review_case_id": "pair-b"}],
        }
    ) == ("pair-a", "pair-b")
