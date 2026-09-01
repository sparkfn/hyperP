"""Bounded secondary-subject construction for CRM repair verification."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_records import RepairSecondaryOutcome
from src.crm_deal_identity_repair.verification_models import (
    RepairSecondaryAction,
    RepairSecondaryDispositionDetail,
    RepairSecondarySubject,
    RepairSecondarySubjectKind,
    RepairVerificationCommand,
)
from src.models import JsonValue


class SecondarySubjectError(RuntimeError):
    """Raised when the frozen/current secondary closure is not exact."""


@dataclass(frozen=True)
class FrozenContextSubject:
    kind: RepairSecondarySubjectKind
    stable_id: str
    evidence: Mapping[str, JsonValue]


def secondary_detail(
    command: RepairVerificationCommand,
    kind: RepairSecondarySubjectKind,
    stable_id: str,
    action: RepairSecondaryAction,
    outcome: RepairSecondaryOutcome,
    *,
    expected: Mapping[str, JsonValue],
    observed: Mapping[str, JsonValue],
) -> RepairSecondaryDispositionDetail:
    """Bind a disposition to bounded pre/post evidence without persisting it."""
    expected_digest = object_digest(
        b"crm-deal-identity-repair-secondary-prestate-v2\x00",
        {"kind": kind, "stable_id": stable_id, "expected": dict(expected)},
    )
    evidence_digest = object_digest(
        b"crm-deal-identity-repair-secondary-evidence-v2\x00",
        {
            "kind": kind,
            "stable_id": stable_id,
            "expected_digest": expected_digest,
            "observed": dict(observed),
            "action": action,
            "outcome": outcome,
        },
    )
    subject = RepairSecondarySubject(kind, stable_id, expected_digest, command.mutation_id)
    return RepairSecondaryDispositionDetail(subject, action, outcome, evidence_digest)


def frozen_context_subjects(payload: Mapping[str, JsonValue]) -> tuple[FrozenContextSubject, ...]:
    """Extract frozen descendant/merge/lock closure subjects from #300 payload evidence."""
    values: list[FrozenContextSubject] = []
    descendants = payload.get("descendants")
    if isinstance(descendants, list):
        for row in descendants:
            if not isinstance(row, dict):
                raise SecondarySubjectError("frozen descendant evidence is malformed")
            stable_id = _string(row.get("source_record_pk"))
            values.append(FrozenContextSubject("descendant", stable_id, row))
    impacts = payload.get("owner_impacts")
    if isinstance(impacts, list):
        for row in impacts:
            if not isinstance(row, dict):
                raise SecondarySubjectError("frozen owner impact evidence is malformed")
            evidence_type = row.get("evidence_type")
            if evidence_type == "merge_lineage":
                values.append(
                    FrozenContextSubject("merge_lineage", _string(row.get("merge_event_id")), row)
                )
            elif evidence_type == "no_match_lock":
                values.append(
                    FrozenContextSubject("no_match_lock", _string(row.get("no_match_lock_id")), row)
                )
    return _unique_context(values)


def assert_current_context(
    expected: Iterable[FrozenContextSubject],
    current: Iterable[FrozenContextSubject],
) -> None:
    expected_values = tuple(expected)
    expected_by_key = {(item.kind, item.stable_id): item.evidence for item in expected_values}
    current_values = tuple(current)
    current_by_key: dict[tuple[str, str], Mapping[str, JsonValue]] = {}
    for item in current_values:
        key = (item.kind, item.stable_id)
        evidence = dict(item.evidence)
        frozen = expected_by_key.get(key)
        if item.kind == "descendant" and frozen is not None:
            if "retired_by_repair_mutation_id" not in frozen:
                evidence.pop("retired_by_repair_mutation_id", None)
        current_by_key[key] = evidence
    if len(current_by_key) != len(current_values) or current_by_key != expected_by_key:
        raise SecondarySubjectError("secondary context closure differs")


def expected_post_repair_context(
    expected: Iterable[FrozenContextSubject], mutation_id: str
) -> tuple[FrozenContextSubject, ...]:
    """Translate frozen #300 descendant evidence to the immutable #309 post-state."""
    values: list[FrozenContextSubject] = []
    for item in expected:
        evidence = dict(item.evidence)
        if item.kind == "descendant":
            relationship_type = evidence.get("relationship_type")
            was_active = evidence.get("relationship_is_active")
            if relationship_type == "LINKED_TO" and was_active is True:
                evidence["relationship_is_active"] = False
                evidence["retired_by_repair_mutation_id"] = mutation_id
        values.append(FrozenContextSubject(item.kind, item.stable_id, evidence))
    return tuple(values)


def override_entries(
    person_id: str, raw: JsonValue
) -> tuple[tuple[str, Mapping[str, JsonValue]], ...]:
    """Return canonical stored override entries with field/source stable identities."""
    if raw is None or raw == "":
        return ()
    value: object = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return ((person_id + ":survivorship_overrides:malformed", {"malformed": True}),)
    if not isinstance(value, dict):
        return ((person_id + ":survivorship_overrides:malformed", {"malformed": True}),)
    entries: list[tuple[str, Mapping[str, JsonValue]]] = []
    for field, candidate in sorted(value.items()):
        if not isinstance(field, str) or not field:
            raise SecondarySubjectError("stored override field is malformed")
        if not isinstance(candidate, dict):
            entries.append((person_id + ":" + field + ":malformed", {"malformed": True}))
            continue
        source_pk = candidate.get("source_record_pk")
        source_identity = source_pk if isinstance(source_pk, str) and source_pk else "custom"
        entries.append((person_id + ":" + field + ":" + source_identity, _json_mapping(candidate)))
    return tuple(entries)


def frozen_pair_case_ids(payload: Mapping[str, JsonValue]) -> tuple[str, ...]:
    """Return only pair-audit cases authenticated by the frozen #300 payload."""
    values: list[str] = []
    for evidence_key in ("decisions_and_reviews", "owner_impacts"):
        evidence = payload.get(evidence_key)
        if not isinstance(evidence, list):
            raise SecondarySubjectError("frozen pair-audit evidence is malformed")
        for item in evidence:
            if not isinstance(item, dict) or item.get("evidence_type") != "pair_audit":
                continue
            case_id = item.get("review_case_id")
            if not isinstance(case_id, str) or not case_id:
                raise SecondarySubjectError("frozen pair-audit evidence is malformed")
            values.append(case_id)
    if len(values) != len(set(values)):
        raise SecondarySubjectError("frozen pair-audit evidence is duplicated")
    return tuple(sorted(values))


def assert_exact_disposition_subjects(
    expected: Iterable[RepairSecondaryDispositionDetail],
    observed_subject_fingerprints: Iterable[str],
) -> None:
    expected_values = tuple(item.subject.fingerprint for item in expected)
    observed_values = tuple(observed_subject_fingerprints)
    if (
        len(expected_values) != len(set(expected_values))
        or len(observed_values) != len(set(observed_values))
        or set(expected_values) != set(observed_values)
    ):
        raise SecondarySubjectError("secondary disposition subject set differs")


def _unique_context(values: list[FrozenContextSubject]) -> tuple[FrozenContextSubject, ...]:
    by_key: dict[tuple[str, str], FrozenContextSubject] = {}
    for value in values:
        key = (value.kind, value.stable_id)
        if key in by_key:
            raise SecondarySubjectError("frozen secondary context is duplicated")
        by_key[key] = value
    return tuple(by_key[key] for key in sorted(by_key))


def _string(value: JsonValue | object) -> str:
    if not isinstance(value, str) or not value:
        raise SecondarySubjectError("frozen secondary identity is malformed")
    return value


def _json_mapping(value: Mapping[object, object]) -> Mapping[str, JsonValue]:
    converted: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise SecondarySubjectError("stored override key is malformed")
        converted[key] = _json_value(item)
    return converted


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return dict(_json_mapping(value))
    raise SecondarySubjectError("stored override value is malformed")
