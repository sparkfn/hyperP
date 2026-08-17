"""Versioned CRM stage mapping and lifecycle policy for authoritative history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from src.models import JsonValue

MappedStageState = Literal["open", "won", "lost", "unresolved", "excluded"]


@dataclass(frozen=True, slots=True, order=True)
class CrmStageTuple:
    entity_type_id: str
    category_id: str | None
    stage_id: str | None
    source_semantic: str | None


@dataclass(frozen=True, slots=True)
class CrmStageMappingEntry:
    stage: CrmStageTuple
    mapped_state: MappedStageState
    reason: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("stage mapping reason must be non-empty")
        if self.mapped_state in {"open", "won", "lost"} and self.stage.stage_id is None:
            raise ValueError("mapped lifecycle stages require a source stage ID")


@dataclass(frozen=True, slots=True)
class CrmStageLifecyclePolicy:
    first_won: Literal["earliest_effective_won"]
    repeated_won: Literal["retain_all_first_is_conversion"]
    reopen: Literal["open_after_won_reopens"]
    revert: Literal["later_effective_state_wins"]
    category_migration: Literal["preserve_event_category"]
    equal_time: Literal["authority_sequence_then_history_id"]


@dataclass(frozen=True, slots=True)
class CrmStageMappingPolicy:
    mapping_version: str
    policy_version: str
    entries: tuple[CrmStageMappingEntry, ...]
    lifecycle: CrmStageLifecyclePolicy
    digest: str

    def __post_init__(self) -> None:
        if not self.mapping_version.strip() or not self.policy_version.strip():
            raise ValueError("mapping and policy versions must be non-empty")
        tuples = tuple(entry.stage for entry in self.entries)
        if len(tuples) != len(set(tuples)):
            raise ValueError("stage mapping contains duplicate tuples")
        if self.digest != mapping_policy_digest(self):
            raise ValueError("stage mapping digest does not match its canonical content")

    def map_stage(self, stage: CrmStageTuple) -> CrmStageMappingEntry | None:
        return next((entry for entry in self.entries if entry.stage == stage), None)


@dataclass(frozen=True, slots=True)
class CrmStageInventoryRow:
    stage: CrmStageTuple
    observation_count: int
    event_identity_count: int
    first_event_at: datetime
    last_event_at: datetime
    effective_count: int
    withheld_count: int


@dataclass(frozen=True, slots=True)
class CrmStageMappingReportRow:
    inventory: CrmStageInventoryRow
    mapped_state: MappedStageState | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class CrmStageMappingReport:
    mapping_version: str
    policy_version: str
    mapping_digest: str
    complete: bool
    observed_tuple_count: int
    mapped_tuple_count: int
    unresolved_tuple_count: int
    excluded_tuple_count: int
    rows: tuple[CrmStageMappingReportRow, ...]


def load_mapping_policy(path: Path) -> CrmStageMappingPolicy:
    """Load and validate an operator-reviewed mapping policy JSON file."""
    try:
        raw = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("CRM stage mapping policy is unreadable") from exc
    if not isinstance(raw, dict):
        raise ValueError("CRM stage mapping policy must be an object")
    mapping_version = _text(raw.get("mapping_version"), "mapping_version")
    policy_version = _text(raw.get("policy_version"), "policy_version")
    entries_raw = raw.get("entries")
    lifecycle_raw = raw.get("lifecycle")
    if not isinstance(entries_raw, list) or not isinstance(lifecycle_raw, dict):
        raise ValueError("CRM stage mapping policy has an invalid shape")
    entries = tuple(_entry(value) for value in entries_raw)
    lifecycle = CrmStageLifecyclePolicy(
        first_won=cast(
            Literal["earliest_effective_won"],
            _literal(lifecycle_raw, "first_won", "earliest_effective_won"),
        ),
        repeated_won=cast(
            Literal["retain_all_first_is_conversion"],
            _literal(lifecycle_raw, "repeated_won", "retain_all_first_is_conversion"),
        ),
        reopen=cast(
            Literal["open_after_won_reopens"],
            _literal(lifecycle_raw, "reopen", "open_after_won_reopens"),
        ),
        revert=cast(
            Literal["later_effective_state_wins"],
            _literal(lifecycle_raw, "revert", "later_effective_state_wins"),
        ),
        category_migration=cast(
            Literal["preserve_event_category"],
            _literal(lifecycle_raw, "category_migration", "preserve_event_category"),
        ),
        equal_time=cast(
            Literal["authority_sequence_then_history_id"],
            _literal(lifecycle_raw, "equal_time", "authority_sequence_then_history_id"),
        ),
    )
    provisional = CrmStageMappingPolicy.__new__(CrmStageMappingPolicy)
    object.__setattr__(provisional, "mapping_version", mapping_version)
    object.__setattr__(provisional, "policy_version", policy_version)
    object.__setattr__(provisional, "entries", entries)
    object.__setattr__(provisional, "lifecycle", lifecycle)
    object.__setattr__(provisional, "digest", "")
    digest = mapping_policy_digest(provisional)
    return CrmStageMappingPolicy(mapping_version, policy_version, entries, lifecycle, digest)


def mapping_policy_digest(policy: CrmStageMappingPolicy) -> str:
    payload = {
        "domain": "hyperp-crm-stage-mapping-policy-v1",
        "mapping_version": policy.mapping_version,
        "policy_version": policy.policy_version,
        "entries": [
            {
                "entity_type_id": item.stage.entity_type_id,
                "category_id": item.stage.category_id,
                "stage_id": item.stage.stage_id,
                "source_semantic": item.stage.source_semantic,
                "mapped_state": item.mapped_state,
                "reason": item.reason,
            }
            for item in sorted(policy.entries, key=lambda value: value.stage)
        ],
        "lifecycle": {
            "first_won": policy.lifecycle.first_won,
            "repeated_won": policy.lifecycle.repeated_won,
            "reopen": policy.lifecycle.reopen,
            "revert": policy.lifecycle.revert,
            "category_migration": policy.lifecycle.category_migration,
            "equal_time": policy.lifecycle.equal_time,
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def build_mapping_report(
    inventory: tuple[CrmStageInventoryRow, ...],
    policy: CrmStageMappingPolicy,
) -> CrmStageMappingReport:
    rows: list[CrmStageMappingReportRow] = []
    for item in sorted(inventory, key=lambda value: value.stage):
        mapping = policy.map_stage(item.stage)
        rows.append(
            CrmStageMappingReportRow(
                inventory=item,
                mapped_state=mapping.mapped_state if mapping is not None else None,
                reason=mapping.reason if mapping is not None else None,
            )
        )
    unmapped = sum(row.mapped_state is None for row in rows)
    unresolved = sum(row.mapped_state == "unresolved" for row in rows)
    excluded = sum(row.mapped_state == "excluded" for row in rows)
    return CrmStageMappingReport(
        mapping_version=policy.mapping_version,
        policy_version=policy.policy_version,
        mapping_digest=policy.digest,
        complete=unmapped == 0,
        observed_tuple_count=len(rows),
        mapped_tuple_count=len(rows) - unmapped,
        unresolved_tuple_count=unresolved,
        excluded_tuple_count=excluded,
        rows=tuple(rows),
    )


def _entry(raw: JsonValue) -> CrmStageMappingEntry:
    if not isinstance(raw, dict):
        raise ValueError("CRM stage mapping entry must be an object")
    mapped = _text(raw.get("mapped_state"), "mapped_state")
    if mapped not in {"open", "won", "lost", "unresolved", "excluded"}:
        raise ValueError("CRM stage mapping state is invalid")
    return CrmStageMappingEntry(
        stage=CrmStageTuple(
            entity_type_id=_text(raw.get("entity_type_id"), "entity_type_id"),
            category_id=_optional_text(raw.get("category_id"), "category_id"),
            stage_id=_optional_text(raw.get("stage_id"), "stage_id"),
            source_semantic=_optional_text(raw.get("source_semantic"), "source_semantic"),
        ),
        mapped_state=cast(MappedStageState, mapped),
        reason=_text(raw.get("reason"), "reason"),
    )


def _literal(payload: dict[str, JsonValue], key: str, expected: str) -> str:
    value = _text(payload.get(key), key)
    if value != expected:
        raise ValueError(f"CRM stage lifecycle policy {key} is unsupported")
    return value


def _text(raw: JsonValue, field_name: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"CRM stage mapping {field_name} must be non-empty")
    return raw.strip()


def _optional_text(raw: JsonValue, field_name: str) -> str | None:
    if raw is None:
        return None
    return _text(raw, field_name)
