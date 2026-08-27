"""Durable status decoding helpers for standalone CRM census runtime control."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal, cast

from src.graph.standalone_crm_census_types import StandaloneCrmPublication
from src.standalone_crm_census_models import FrozenSourceWindow, StandaloneCrmAttempt


def int_field(value: dict[str, object], key: str) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise RuntimeError(f"invalid durable census {key}")
    return raw


def text_field(value: dict[str, object], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"invalid durable census {key}")
    return raw


def attempt_from_status(value: dict[str, object]) -> StandaloneCrmAttempt:
    required = ("census_id", "task_id", "state", "deadline_at", "occurrence_deadline_at")
    if not all(isinstance(value.get(key), str) for key in required):
        raise RuntimeError("invalid durable census attempt")
    deadline = datetime.fromisoformat(cast(str, value["deadline_at"]).replace("Z", "+00:00"))
    occurrence = datetime.fromisoformat(
        cast(str, value["occurrence_deadline_at"]).replace("Z", "+00:00")
    )
    return StandaloneCrmAttempt(
        cast(str, value["census_id"]),
        int_field(value, "generation"),
        cast(str, value["task_id"]),
        cast(
            Literal[
                "queued", "running", "paused_with_checkpoint", "failed", "superseded", "completed"
            ],
            value["state"],
        ),
        int_field(value, "fence_token"),
        deadline.astimezone(UTC),
        occurrence.astimezone(UTC),
    )


def frozen_window(census: dict[str, object]) -> FrozenSourceWindow | None:
    raw = census.get("source_window_json")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise RuntimeError("persisted source window is invalid")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("persisted source window is invalid")
    selected = parsed.get("selected_kinds")
    bounds = parsed.get("upper_bounds")
    algorithm = parsed.get("algorithm_version")
    if (
        not isinstance(selected, list)
        or not isinstance(bounds, list)
        or not isinstance(algorithm, str)
        or not all(isinstance(kind, str) for kind in selected)
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], int)
            and not isinstance(item[1], bool)
            for item in bounds
        )
    ):
        raise RuntimeError("persisted source window is invalid")
    return FrozenSourceWindow(
        tuple(cast(Literal["contact", "lead", "company"], kind) for kind in selected),
        tuple((cast(Literal["contact", "lead", "company"], item[0]), item[1]) for item in bounds),
        algorithm,
    )


def publication_generation(publication: StandaloneCrmPublication) -> int:
    return payload_generation(publication.payload_json)


def payload_generation(payload_json: str) -> int:
    payload = _payload(payload_json)
    generation = payload.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise RuntimeError("stored child payload generation is invalid")
    return generation


def payload_unit_kind(payload_json: str) -> str:
    unit_kind = _payload(payload_json).get("unit_kind")
    if unit_kind not in {"contact", "lead", "company", "mapping_prepare", "mapping_rollback"}:
        raise RuntimeError("stored child payload unit kind is invalid")
    return unit_kind


def _payload(payload_json: str) -> dict[str, object]:
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise RuntimeError("stored child payload is invalid")
    return cast(dict[str, object], payload)
