"""Derived-state reads and guarded reconciliation actions for CRM repair verification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.crm_deal_identity_repair.digests import derived_state_digest
from src.crm_deal_identity_repair.execution_models import RepairSecondaryDisposition
from src.crm_deal_identity_repair.verification_models import (
    RepairSecondaryAction,
    RepairSecondaryDispositionDetail,
    RepairSecondarySubjectKind,
    RepairVerificationCommand,
)
from src.graph.crm_deal_identity_repair_verification_errors import RepairVerificationDriftError
from src.graph.crm_deal_identity_repair_verification_records import VerificationBundle
from src.graph.crm_deal_identity_repair_verification_secondary import (
    FrozenContextSubject,
    SecondarySubjectError,
    assert_current_context,
    expected_post_repair_context,
    frozen_context_subjects,
    frozen_pair_case_ids,
    override_entries,
    secondary_detail,
)
from src.graph.crm_deal_identity_repair_verification_support import (
    json_mapping,
    json_value,
    optional_nonnegative_int,
    required_record_int,
    required_row_string,
    retired_source_record_pks,
)
from src.graph.queries import crm_deal_identity_repair_verification as queries
from src.identity_link_revisions import IdentityLinkDesiredRevision, append_identity_link_revisions
from src.models import JsonValue


def affected_person_ids(tx: ManagedTransaction, pks: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        str(row["person_id"])
        for row in tx.run(queries.READ_AFFECTED_PERSON_IDS, source_record_pks=list(pks))
    )


def accepted_input_person_ids(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    bundle: VerificationBundle,
) -> tuple[str, ...]:
    """Return active People whose accepted evidence changed in this mutation only."""
    from src.graph.crm_deal_identity_repair_verification_support import retirement_requirements

    values: set[str] = set()
    for requirement in retirement_requirements(command, bundle):
        if not requirement["frozen_active"]:
            continue
        if requirement["relationship_type"] == "LINKED_TO":
            person_id = _identity_person_id(requirement["right_identity"])
        else:
            person_id = _identity_person_id(requirement["left_identity"])
        if person_id is not None:
            values.add(person_id)
    if bundle.result.outcome == "applied":
        row = tx.run(
            queries.READ_APPLIED_REPLACEMENT_OWNER,
            source_record_pk=bundle.replacement_pk,
            mutation_id=command.mutation_id,
        ).single()
        if row is None:
            raise RepairVerificationDriftError("applied replacement owner is missing")
        values.add(required_row_string(row, "person_id"))
    rows = tuple(tx.run(queries.READ_ACTIVE_PERSON_IDS, person_ids=sorted(values)))
    return tuple(required_row_string(row, "person_id") for row in rows)


def _identity_person_id(identity: Mapping[str, JsonValue]) -> str | None:
    if identity.get("key") != "person_id":
        return None
    value = identity.get("value")
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True)
class PersonDerivedState:
    person_id: str
    crm_deal_count: int
    analysis_revision: int
    overrides: JsonValue
    profile: Mapping[str, JsonValue]


def read_person_states(
    tx: ManagedTransaction, person_ids: tuple[str, ...]
) -> tuple[PersonDerivedState, ...]:
    if not person_ids:
        return ()
    values: list[PersonDerivedState] = []
    for row in tx.run(queries.READ_AFFECTED_PERSON_DERIVED_STATE, person_ids=list(person_ids)):
        person_id = required_row_string(row, "person_id")
        count = required_record_int(row, "crm_deal_count")
        revision = optional_nonnegative_int(row, "analysis_input_revision")
        profile = {
            key: json_value(row[key])
            for key in (
                "preferred_full_name",
                "preferred_dob",
                "preferred_phone",
                "preferred_email",
                "preferred_address_id",
                "preferred_nric",
                "preferred_race_ethnicity",
                "profile_completeness_score",
                "golden_profile_version",
            )
        }
        values.append(
            PersonDerivedState(
                person_id, count, revision, json_value(row["survivorship_overrides"]), profile
            )
        )
    if tuple(value.person_id for value in values) != tuple(sorted(person_ids)):
        raise RepairVerificationDriftError("affected active Person closure differs")
    return tuple(values)


def build_person_details(
    command: RepairVerificationCommand,
    states: tuple[PersonDerivedState, ...],
    conflicts: Mapping[str, tuple[str, ...]],
) -> list[RepairSecondaryDispositionDetail]:
    details: list[RepairSecondaryDispositionDetail] = []
    for state in states:
        person_evidence: dict[str, JsonValue] = {
            "person_id": state.person_id,
            "crm_deal_count": state.crm_deal_count,
            "profile": dict(state.profile),
        }
        details.append(
            secondary_detail(
                command,
                "crm_deal_count",
                state.person_id,
                "recomputed",
                "reconciled",
                expected={"person_id": state.person_id},
                observed=person_evidence,
            )
        )
        details.append(
            secondary_detail(
                command,
                "golden_profile",
                state.person_id,
                "recomputed",
                "reconciled",
                expected={"person_id": state.person_id},
                observed=person_evidence,
            )
        )
        conflict_fields = set(conflicts.get(state.person_id, ()))
        for stable_id, override in override_entries(state.person_id, state.overrides):
            field = stable_id.split(":", 2)[1] if stable_id.count(":") >= 2 else "malformed"
            conflict = field in conflict_fields or override.get("malformed") is True
            details.append(
                secondary_detail(
                    command,
                    "survivorship_override",
                    stable_id,
                    "conflict_preserved" if conflict else "preserved",
                    "review_required" if conflict else "reconciled",
                    expected={"person_id": state.person_id, "override": dict(override)},
                    observed={"profile": dict(state.profile), "conflict": conflict},
                )
            )
    return details


def expected_subject_keys(
    command: RepairVerificationCommand,
    states: tuple[PersonDerivedState, ...],
    replacement_pk: str,
    invalidated_person_ids: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Derive the read-only exact secondary subject closure without action writes."""
    values: list[tuple[str, str]] = []
    for state in states:
        values.extend(
            (
                ("crm_deal_count", state.person_id),
                ("golden_profile", state.person_id),
            )
        )
        values.extend(
            ("survivorship_override", stable_id)
            for stable_id, _ in override_entries(state.person_id, state.overrides)
        )
    values.extend(
        ("profile_analysis_invalidation", person_id) for person_id in invalidated_person_ids
    )
    context_subjects = frozen_context_subjects(command.inventory.payload)
    values.extend((item.kind, item.stable_id) for item in context_subjects)
    pair_case_ids = frozen_pair_case_ids(command.inventory.payload)
    values.extend(("pair_audit_case", case_id) for case_id in pair_case_ids)
    values.append(("identity_link_revision", replacement_pk))
    if len(values) != len(set(values)):
        raise RepairVerificationDriftError("expected secondary subject closure is duplicate")
    return tuple(sorted(values))


def build_context_details(
    tx: ManagedTransaction, command: RepairVerificationCommand
) -> list[RepairSecondaryDispositionDetail]:
    frozen = frozen_context_subjects(command.inventory.payload)
    expected = expected_post_repair_context(frozen, command.mutation_id)
    current_rows = tuple(
        FrozenContextSubject(
            _context_kind(row),
            required_row_string(row, "stable_id"),
            json_mapping(row, "evidence"),
        )
        for row in tx.run(
            queries.READ_SECONDARY_CONTEXT,
            source_record_pks=list(retired_source_record_pks(command)),
        )
    )
    try:
        assert_current_context(expected, current_rows)
    except SecondarySubjectError as exc:
        raise RepairVerificationDriftError("secondary context closure differs") from exc
    return [
        secondary_detail(
            command,
            item.kind,
            item.stable_id,
            "verified_exact",
            "reconciled",
            expected=item.evidence,
            observed=current.evidence,
        )
        for item, current in zip(expected, current_rows, strict=True)
    ]


def _context_kind(row: Record) -> RepairSecondarySubjectKind:
    value = required_row_string(row, "kind")
    if value == "descendant":
        return "descendant"
    if value == "merge_lineage":
        return "merge_lineage"
    if value == "no_match_lock":
        return "no_match_lock"
    raise RepairVerificationDriftError("secondary context kind is malformed")


def _replacement_effective_at(
    tx: ManagedTransaction, command: RepairVerificationCommand, new_pk: str
) -> str:
    row = tx.run(
        queries.READ_REPLACEMENT_EFFECTIVE_TIME,
        source_record_pk=new_pk,
        mutation_id=command.mutation_id,
    ).single()
    if row is None:
        raise RepairVerificationDriftError("replacement effective time is missing")
    return required_row_string(row, "effective_at")


def reconcile_identity_link_revision(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    new_pk: str,
    outcome: str,
) -> RepairSecondaryDispositionDetail:
    desired = _desired_identity_link_revision(tx, command, new_pk, outcome)
    rows = append_identity_link_revisions(tx, [desired])
    _assert_exact_identity_link_revision(tx, desired)
    action: RepairSecondaryAction = "appended_revision" if rows else "no_op"
    return secondary_detail(
        command,
        "identity_link_revision",
        new_pk,
        action,
        "reconciled",
        expected={
            "deal_id": command.inventory.deal_id,
            "outcome": outcome,
            "owner_id": desired.hyperp_person_id,
            "cause_key": desired.cause_key,
        },
        observed={"effective_at": desired.effective_at, "appended": bool(rows)},
    )


def verify_replayed_revision(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    new_pk: str,
    outcome: str,
) -> None:
    desired = _desired_identity_link_revision(tx, command, new_pk, outcome)
    _assert_exact_identity_link_revision(tx, desired)


def _desired_identity_link_revision(
    tx: ManagedTransaction,
    command: RepairVerificationCommand,
    new_pk: str,
    outcome: str,
) -> IdentityLinkDesiredRevision:
    owner_id: str | None = None
    effective_at = _replacement_effective_at(tx, command, new_pk)
    if outcome == "applied":
        owner = tx.run(
            queries.READ_APPLIED_REPLACEMENT_OWNER,
            source_record_pk=new_pk,
            mutation_id=command.mutation_id,
        ).single()
        if owner is None:
            raise RepairVerificationDriftError("applied replacement owner is missing")
        owner_id = required_row_string(owner, "person_id")
        owner_effective_at = owner["effective_at"]
        if isinstance(owner_effective_at, str) and owner_effective_at:
            effective_at = owner_effective_at
    return IdentityLinkDesiredRevision(
        "bitrix_chat",
        command.source_instance_id,
        "deal",
        command.inventory.deal_id,
        "crm_deal_identity_v2",
        "resolved" if outcome == "applied" else "pending_review",
        owner_id,
        "source_supersession",
        effective_at,
        command.mutation_id + ":crm_deal_identity_v2",
    )


def _assert_exact_identity_link_revision(
    tx: ManagedTransaction,
    desired: IdentityLinkDesiredRevision,
) -> None:
    rows = tuple(tx.run(queries.READ_IDENTITY_LINK_REVISION_CAUSE, cause_key=desired.cause_key))
    if len(rows) != 1:
        raise RepairVerificationDriftError("identity-link revision cardinality differs")
    row = rows[0]
    expected: dict[str, str | None] = {
        "source_system": desired.source_system,
        "source_instance_id": desired.source_instance_id,
        "source_entity_type": desired.source_entity_type,
        "source_entity_id": desired.source_entity_id,
        "identity_policy_version": desired.identity_policy_version,
        "link_status": desired.link_status,
        "person_id": desired.hyperp_person_id,
        "resolution_kind": desired.resolution_kind,
        "effective_at": desired.effective_at,
        "cause_key": desired.cause_key,
    }
    for key, expected_value in expected.items():
        if row[key] != expected_value:
            raise RepairVerificationDriftError("identity-link revision differs")


def build_invalidation_details(
    command: RepairVerificationCommand,
    before: Mapping[str, int],
    after: tuple[PersonDerivedState, ...],
) -> list[RepairSecondaryDispositionDetail]:
    details: list[RepairSecondaryDispositionDetail] = []
    for state in after:
        previous = before.get(state.person_id)
        if previous is None or state.analysis_revision != previous + 1:
            raise RepairVerificationDriftError("profile-analysis invalidation evidence differs")
        details.append(
            secondary_detail(
                command,
                "profile_analysis_invalidation",
                state.person_id,
                "invalidated_once",
                "reconciled",
                expected={"person_id": state.person_id, "analysis_revision": previous},
                observed={"analysis_revision": state.analysis_revision},
            )
        )
    return details


def assert_no_existing_dispositions(
    tx: ManagedTransaction, command: RepairVerificationCommand
) -> None:
    if (
        (
            row := tx.run(
                queries.READ_EXISTING_VERIFICATION_DISPOSITIONS,
                run_id=command.unit.run_id,
                unit_id=command.unit.unit_id,
            ).single()
        )
        is None
        or required_record_int(row, "verification_count") != 0
        or required_record_int(row, "disposition_count") != 0
    ):
        raise RepairVerificationDriftError("pending verification already has dispositions")


def canonical_details(
    values: list[RepairSecondaryDispositionDetail],
) -> list[RepairSecondaryDispositionDetail]:
    ordered = sorted(values, key=lambda item: (item.subject.kind, item.subject.stable_id))
    fingerprints = [item.subject.fingerprint for item in ordered]
    if len(fingerprints) != len(set(fingerprints)):
        raise RepairVerificationDriftError("verification secondary subjects are duplicate")
    return ordered


def derive_state_digest(
    primary: Record,
    records: tuple[RepairSecondaryDisposition, ...],
    states: tuple[PersonDerivedState, ...],
    pairs: tuple[Mapping[str, JsonValue], ...],
) -> str:
    return derived_state_digest(
        {
            "primary": {
                key: required_record_int(primary, key)
                for key in (
                    "active_links",
                    "active_any_links",
                    "provisional_links",
                    "all_links",
                    "active_new_evidence",
                    "repair_review_count",
                    "repair_decision_count",
                    "retired_relationship_count",
                    "retirement_stamp_failure_count",
                    "forbidden_projection_count",
                )
            },
            "persons": [
                {
                    "person_id": state.person_id,
                    "crm_deal_count": state.crm_deal_count,
                    "analysis_revision": state.analysis_revision,
                    "overrides": state.overrides,
                    "profile": dict(state.profile),
                }
                for state in states
            ],
            "pairs": [dict(pair) for pair in pairs],
            "dispositions": [
                {
                    "disposition_id": item.disposition_id,
                    "subject_fingerprint": item.subject_fingerprint,
                    "evidence_digest": item.evidence_digest,
                    "payload_digest": item.payload_digest,
                    "outcome": item.outcome,
                }
                for item in records
            ],
        }
    )
