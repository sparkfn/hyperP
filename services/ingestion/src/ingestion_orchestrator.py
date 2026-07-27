"""Validated all-source ingestion manifest parsing and CLI helpers."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from celery import Celery
from pydantic.types import JsonValue

from src.config import get_settings
from src.connectors.whatsadmin_api.credentials import WHATSADMIN_ENTITIES

TaskMode = Literal["batch", "dump", "api", "backfill"]

INGESTION_QUEUE = "ingestion"
ORCHESTRATION_PRIORITY = 0
IDENTITY_TASK_PRIORITY = 1
DEPENDENT_TASK_PRIORITY = 5

_START_ORCHESTRATED_INGESTION_TASK = (
    "src.ingestion_orchestration_tasks.start_orchestrated_ingestion_task"
)

_IDENTITY_SOURCE_KEYS = frozenset(
    {
        "fundbox",
        "fundbox:legacy",
        "fundbox:merged",
        "eko_phppos",
        "speedzone_phppos",
        "onediver",
    }
)
_DEPENDENT_SOURCE_KEYS = frozenset(
    {
        "fundbox:contacts",
        "fundbox:sales",
        "eko_phppos:sales",
        "speedzone_phppos:sales",
        "onediver:sales",
        "bitrix_chat",
        "whatsapp_chat",
        "sgbankruptcy",
        "sgrentalflats",
    }
)
_ALL_SOURCE_KEYS = _IDENTITY_SOURCE_KEYS | _DEPENDENT_SOURCE_KEYS
_DUMP_SOURCE_KEYS = frozenset(_ALL_SOURCE_KEYS)
_BATCH_SOURCE_KEYS = frozenset(
    {
        "fundbox",
        "fundbox:contacts",
        "fundbox:legacy",
        "fundbox:merged",
        "fundbox:sales",
        "eko_phppos",
        "eko_phppos:sales",
        "speedzone_phppos",
        "speedzone_phppos:sales",
        "whatsapp_chat",
        "bitrix_chat",
    }
)
_API_SOURCE_KEYS = frozenset(
    {
        "fundbox",
        "fundbox:contacts",
        "fundbox:sales",
        "eko_phppos",
        "eko_phppos:sales",
        "speedzone_phppos",
        "speedzone_phppos:sales",
        "whatsapp_chat",
        "bitrix_chat",
        "sgbankruptcy",
        "sgrentalflats",
    }
)
_ALLOWED_TASK_FIELDS = frozenset({"source_key", "mode", "dump_path", "entity_key", "priority"})
_ALLOWED_MANIFEST_FIELDS = frozenset({"identity", "dependent"})


class ManifestValidationError(ValueError):
    """Raised when an all-source ingestion payload is invalid."""


@dataclass(frozen=True)
class IngestionTaskSpec:
    """One source task in a phase of an all-source ingestion."""

    source_key: str
    mode: TaskMode
    dump_path: str | None
    entity_key: str | None
    priority: int

    def to_payload(self) -> dict[str, JsonValue]:
        """Return JSON-safe arguments for the Celery orchestration task."""
        return {
            "source_key": self.source_key,
            "mode": self.mode,
            "dump_path": self.dump_path,
            "entity_key": self.entity_key,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class IngestionManifest:
    """Complete two-phase one-time ingestion plan."""

    identity: tuple[IngestionTaskSpec, ...]
    dependent: tuple[IngestionTaskSpec, ...]

    def to_payload(self) -> dict[str, JsonValue]:
        """Return JSON-safe arguments for the Celery orchestration task."""
        return {
            "identity": [task.to_payload() for task in self.identity],
            "dependent": [task.to_payload() for task in self.dependent],
        }


def _invalid(path: str, message: str) -> ManifestValidationError:
    return ManifestValidationError(f"{path}: {message}")


def _mapping(raw: JsonValue, path: str) -> dict[str, JsonValue]:
    if not isinstance(raw, dict):
        raise _invalid(path, "must be an object")
    return raw


def _string(raw: JsonValue, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid(path, "must be a non-empty string")
    return raw.strip()


def _optional_string(raw: JsonValue, path: str) -> str | None:
    if raw is None:
        return None
    return _string(raw, path)


def _parse_dump_path(raw: JsonValue, path: str) -> str:
    value = _string(raw, path).replace("\\", "/")
    normalized = PurePosixPath(value)
    has_windows_anchor = bool(normalized.parts) and normalized.parts[0].endswith(":")
    if (
        normalized.as_posix() == "."
        or normalized.is_absolute()
        or has_windows_anchor
        or ".." in normalized.parts
    ):
        raise _invalid(path, "must be relative to DUMPS_ROOT without parent traversal")
    return normalized.as_posix()


def _parse_priority(raw: JsonValue, path: str, default: int) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > 9:
        raise _invalid(path, "must be an integer from 0 through 9")
    return raw


def _parse_task(raw: JsonValue, phase: str, index: int) -> IngestionTaskSpec:
    path = f"{phase}[{index}]"
    item = _mapping(raw, path)
    unknown_fields = set(item) - _ALLOWED_TASK_FIELDS
    if unknown_fields:
        raise _invalid(path, f"contains unknown field(s): {', '.join(sorted(unknown_fields))}")
    source_key = _string(item.get("source_key"), f"{path}.source_key")
    allowed_sources = _IDENTITY_SOURCE_KEYS if phase == "identity" else _DEPENDENT_SOURCE_KEYS
    if source_key not in allowed_sources:
        raise _invalid(f"{path}.source_key", f"is not valid for the {phase} phase")
    raw_mode = _string(item.get("mode"), f"{path}.mode")
    if raw_mode not in {"batch", "dump", "api", "backfill"}:
        raise _invalid(f"{path}.mode", "must be batch, dump, api, or backfill")
    mode = cast(TaskMode, raw_mode)
    raw_dump_path = item.get("dump_path")
    if mode == "dump":
        dump_path = _parse_dump_path(raw_dump_path, f"{path}.dump_path")
        if source_key not in _DUMP_SOURCE_KEYS:
            raise _invalid(f"{path}.source_key", "does not support dump mode")
    else:
        if raw_dump_path is not None:
            raise _invalid(f"{path}.dump_path", "is only valid when mode is dump")
        dump_path = None
    supported_sources = {
        "batch": _BATCH_SOURCE_KEYS,
        "dump": _DUMP_SOURCE_KEYS,
        "api": _API_SOURCE_KEYS,
        "backfill": frozenset({"bitrix_chat"}),
    }[mode]
    if source_key not in supported_sources:
        raise _invalid(f"{path}.mode", f"{mode} is not supported for {source_key}")
    entity_key = _optional_string(item.get("entity_key"), f"{path}.entity_key")
    if entity_key is not None and (source_key != "whatsapp_chat" or mode != "api"):
        raise _invalid(f"{path}.entity_key", "is only valid for whatsapp_chat API ingestion")
    if entity_key is not None and entity_key not in WHATSADMIN_ENTITIES:
        allowed_entities = ", ".join(WHATSADMIN_ENTITIES)
        raise _invalid(f"{path}.entity_key", f"must be one of: {allowed_entities}")
    default_priority = IDENTITY_TASK_PRIORITY if phase == "identity" else DEPENDENT_TASK_PRIORITY
    priority = _parse_priority(item.get("priority"), f"{path}.priority", default_priority)
    return IngestionTaskSpec(source_key, mode, dump_path, entity_key, priority)


def parse_manifest(payload: str) -> IngestionManifest:
    """Parse and validate a complete two-phase all-source JSON payload."""
    try:
        loaded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ManifestValidationError(f"payload: invalid JSON ({exc.msg})") from exc
    raw = cast(JsonValue, loaded)
    manifest = _mapping(raw, "payload")
    unknown_fields = set(manifest) - _ALLOWED_MANIFEST_FIELDS
    if unknown_fields:
        raise _invalid("payload", f"contains unknown field(s): {', '.join(sorted(unknown_fields))}")
    identity_raw = manifest.get("identity")
    dependent_raw = manifest.get("dependent")
    if not isinstance(identity_raw, list):
        raise _invalid("payload.identity", "must be an array")
    if not isinstance(dependent_raw, list):
        raise _invalid("payload.dependent", "must be an array")
    identity = tuple(
        _parse_task(item, "identity", index) for index, item in enumerate(identity_raw)
    )
    dependent = tuple(
        _parse_task(item, "dependent", index) for index, item in enumerate(dependent_raw)
    )
    seen_source_keys = [task.source_key for task in (*identity, *dependent)]
    if len(set(seen_source_keys)) != len(seen_source_keys):
        raise ManifestValidationError("payload: source_key entries must be unique")
    missing_source_keys = _ALL_SOURCE_KEYS - set(seen_source_keys)
    extra_source_keys = set(seen_source_keys) - _ALL_SOURCE_KEYS
    if missing_source_keys or extra_source_keys:
        details: list[str] = []
        if missing_source_keys:
            details.append(f"missing: {', '.join(sorted(missing_source_keys))}")
        if extra_source_keys:
            details.append(f"unknown: {', '.join(sorted(extra_source_keys))}")
        detail_text = "; ".join(details)
        raise ManifestValidationError(
            f"payload: must contain every source exactly once ({detail_text})"
        )
    return IngestionManifest(identity=identity, dependent=dependent)


def _payload_from_args(args: argparse.Namespace) -> str:
    provided = sum(value is not None for value in (args.payload, args.manifest))
    provided += int(args.payload_stdin)
    if provided != 1:
        raise ManifestValidationError(
            "provide exactly one of --payload, --payload-stdin, or --manifest"
        )
    if args.payload is not None:
        return cast(str, args.payload)
    if args.payload_stdin:
        return sys.stdin.read()
    manifest_path = cast(str, args.manifest)
    try:
        return Path(manifest_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestValidationError(f"manifest: could not read {manifest_path}: {exc}") from exc


def _write_json(payload: dict[str, JsonValue]) -> None:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Queue a two-phase all-source ingestion.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "trigger"):
        subcommand = subcommands.add_parser(command)
        input_group = subcommand.add_mutually_exclusive_group(required=True)
        input_group.add_argument("--payload")
        input_group.add_argument("--payload-stdin", action="store_true")
        input_group.add_argument("--manifest")
    return parser


def main() -> int:
    """Run the agent-friendly all-source ingestion command-line interface."""
    args = _build_parser().parse_args()
    try:
        manifest = parse_manifest(_payload_from_args(args))
    except ManifestValidationError as exc:
        _write_json({"status": "invalid", "error": str(exc)})
        return 2
    if args.command == "validate":
        _write_json(
            {
                "status": "valid",
                "identity_task_count": len(manifest.identity),
                "dependent_task_count": len(manifest.dependent),
            }
        )
        return 0
    app = Celery(
        "profile_unifier_ingestion_cli",
        broker=get_settings().celery_broker_url,
        backend=None,
    )
    try:
        result = app.send_task(
            _START_ORCHESTRATED_INGESTION_TASK,
            args=(manifest.to_payload(),),
            queue=INGESTION_QUEUE,
            priority=ORCHESTRATION_PRIORITY,
        )
    except Exception:
        _write_json({"status": "queue_failed", "error": "broker submission failed"})
        return 1
    _write_json(
        {
            "status": "queued",
            "orchestration_id": str(result.id),
            "celery_task_id": str(result.id),
            "identity_task_count": len(manifest.identity),
            "dependent_task_count": len(manifest.dependent),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
