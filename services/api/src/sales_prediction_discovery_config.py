"""Validated optional discovery mappings for CRM win feasibility reports."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

_ENTITY_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TAXONOMY_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
_APPROVAL_REFERENCE = re.compile(r"^(?:issue|pr)-[0-9]{1,10}$|^#[0-9]{1,10}$")


@dataclass(frozen=True)
class EntityStagePolicy:
    """A discovery-only, externally approved stage-policy claim."""

    entity_key: str
    open_stage_ids: tuple[str, ...]
    won_stage_ids: tuple[str, ...]
    lost_stage_ids: tuple[str, ...]
    excluded_stage_ids: tuple[str, ...]
    reopen_revert_policy_status: str


@dataclass(frozen=True)
class StageMapping:
    """Validated mapping input and its non-authoritative approval metadata."""

    policy_version: str
    claimed_approval_status: str
    external_approval_reference: str | None
    entities: tuple[EntityStagePolicy, ...]
    configuration_hash: str

    @property
    def approval_status(self) -> str:
        """A local JSON artifact cannot verify an external approval decision."""
        return "approval_unverified"


def load_stage_mapping(path: Path) -> StageMapping:
    """Load a small discovery mapping without treating self-claims as approval."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read stage mapping: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("stage mapping must be valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("stage mapping must be a JSON object")

    policy_version = _required_taxonomy(raw, "policy_version")
    claimed_approval_status = _required_choice(
        raw, "claimed_approval_status", {"approved", "draft"}
    )
    approval_reference = _optional_reference(raw.get("external_approval_reference"))
    raw_entities = raw.get("entities")
    if not isinstance(raw_entities, list) or not raw_entities:
        raise ValueError("stage mapping must contain a non-empty entities list")
    entities = tuple(_parse_entity(item) for item in raw_entities)
    entity_keys = [item.entity_key for item in entities]
    if len(entity_keys) != len(set(entity_keys)):
        raise ValueError("stage mapping must not repeat an entity key")

    canonical = {
        "claimed_approval_status": claimed_approval_status,
        "entities": [
            {
                "entity_key": item.entity_key,
                "excluded_stage_ids": list(item.excluded_stage_ids),
                "lost_stage_ids": list(item.lost_stage_ids),
                "open_stage_ids": list(item.open_stage_ids),
                "reopen_revert_policy_status": item.reopen_revert_policy_status,
                "won_stage_ids": list(item.won_stage_ids),
            }
            for item in sorted(entities, key=lambda item: item.entity_key)
        ],
        "external_approval_reference": approval_reference,
        "policy_version": policy_version,
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return StageMapping(
        policy_version=policy_version,
        claimed_approval_status=claimed_approval_status,
        external_approval_reference=approval_reference,
        entities=tuple(sorted(entities, key=lambda item: item.entity_key)),
        configuration_hash=digest,
    )


def _parse_entity(value: object) -> EntityStagePolicy:
    if not isinstance(value, dict):
        raise ValueError("each mapping entity must be an object")
    entity_key = _required_entity_key(value)
    open_stage_ids = _stage_ids(value, "open_stage_ids", required=True)
    won_stage_ids = _stage_ids(value, "won_stage_ids", required=True)
    lost_stage_ids = _stage_ids(value, "lost_stage_ids", required=False)
    excluded_stage_ids = _stage_ids(value, "excluded_stage_ids", required=False)
    groups = (open_stage_ids, won_stage_ids, lost_stage_ids, excluded_stage_ids)
    combined = [stage for group in groups for stage in group]
    if len(combined) != len(set(combined)):
        raise ValueError(f"stage mapping for {entity_key} has overlapping stage IDs")
    return EntityStagePolicy(
        entity_key=entity_key,
        open_stage_ids=open_stage_ids,
        won_stage_ids=won_stage_ids,
        lost_stage_ids=lost_stage_ids,
        excluded_stage_ids=excluded_stage_ids,
        reopen_revert_policy_status=_required_choice(
            value, "reopen_revert_policy_status", {"defined", "pending"}
        ),
    )


def _required_entity_key(value: dict[str, object]) -> str:
    entity_key = value.get("entity_key")
    if not isinstance(entity_key, str) or _ENTITY_KEY.fullmatch(entity_key) is None:
        raise ValueError("mapping entity_key must use lowercase entity-key syntax")
    return entity_key


def _stage_ids(value: dict[str, object], key: str, *, required: bool) -> tuple[str, ...]:
    raw = value.get(key)
    if raw is None and not required:
        return ()
    if not isinstance(raw, list) or (required and not raw):
        raise ValueError(f"{key} must be a non-empty list" if required else f"{key} must be a list")
    stages = tuple(_taxonomy_item(item, key) for item in raw)
    if len(stages) != len(set(stages)):
        raise ValueError(f"{key} must not repeat a stage ID")
    return tuple(sorted(stages))


def _required_taxonomy(value: dict[str, object], key: str) -> str:
    return _taxonomy_item(value.get(key), key)


def _taxonomy_item(value: object, key: str) -> str:
    if not isinstance(value, str) or _TAXONOMY_VALUE.fullmatch(value) is None:
        raise ValueError(f"{key} must be a bounded taxonomy value")
    return value


def _required_choice(value: dict[str, object], key: str, allowed: set[str]) -> str:
    item = value.get(key)
    if not isinstance(item, str) or item not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{key} must be one of: {choices}")
    return item


def _optional_reference(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _APPROVAL_REFERENCE.fullmatch(value) is None:
        raise ValueError("external_approval_reference must be an issue or PR reference")
    return value
