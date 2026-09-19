#!/usr/bin/env python3
"""Safe deployment-only scheduled-ingestion policy inspection and staging."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path, PurePosixPath
from typing import Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SERVICES: Final[tuple[str, ...]] = ("ingestion-worker", "lifecycle-worker", "beat")
_TARGET: Final[str] = "/app/config"
_POLICY: Final[dict[str, object]] = {
    "timezone": "Asia/Singapore",
    "local_opening": "09:00",
    "local_cutoff": "23:00",
    "drain_reserve_seconds": 900,
}
_POLICY_ENV: Final[dict[str, str]] = {
    "SCHEDULED_INGESTION_TIMEZONE": "Asia/Singapore",
    "SCHEDULED_INGESTION_LOCAL_OPENING": "09:00",
    "SCHEDULED_INGESTION_LOCAL_CUTOFF": "23:00",
    "SCHEDULED_INGESTION_DRAIN_RESERVE_SECONDS": "900",
}
_CEILINGS: Final[dict[str, tuple[float, int, int]]] = {
    "ingestion-worker": (1.0, 2 * 1024**3, 900),
    "lifecycle-worker": (0.5, 1024**3, 900),
    "beat": (0.25, 256 * 1024**2, 900),
}
_NESTED_CONFIG_SECTIONS: Final[frozenset[str]] = frozenset(
    {
        "exclusions",
        "llm",
        "bitrix_openlines",
        "scheduled_ingestion",
        "crm_tenant_mapping_authorization",
        "stage_history_ingestion",
    }
)


class PolicyError(RuntimeError):
    """A policy input is unsafe or incompatible with the deployment contract."""


@dataclass(frozen=True)
class EffectiveConfig:
    """Verified configuration and deployment policy shared by worker services."""

    path: Path
    enabled: bool
    timezone: str
    opening: str
    cutoff: str
    reserve_seconds: int
    needs_migration: bool
    legacy_bare_exclusions: bool
    payload: dict[str, object]


def _mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PolicyError(message)
    return cast(dict[str, object], value)


def _environment(service: dict[str, object]) -> dict[str, str]:
    raw = service.get("environment", {})
    if isinstance(raw, list):
        values: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, str) or "=" not in item:
                raise PolicyError("resolved worker environment is malformed")
            key, value = item.split("=", 1)
            values[key] = value
        return values
    values = _mapping(raw, "resolved worker environment is malformed")
    result: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, (str, int, float, bool)):
            raise PolicyError("resolved worker environment is malformed")
        result[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def _bind_root(service: dict[str, object], directory: Path) -> Path:
    volumes = service.get("volumes", [])
    if not isinstance(volumes, list):
        raise PolicyError("resolved worker volumes are malformed")
    roots: list[Path] = []
    for raw in volumes:
        source: str | None = None
        target: str | None = None
        kind: str | None = None
        if isinstance(raw, str):
            parts = raw.split(":")
            if len(parts) >= 2:
                source, target, kind = parts[0], parts[1], "bind"
        elif isinstance(raw, dict):
            volume = _mapping(raw, "resolved worker volume is malformed")
            raw_source, raw_target, raw_kind = (
                volume.get("source"),
                volume.get("target"),
                volume.get("type"),
            )
            if isinstance(raw_source, str) and isinstance(raw_target, str):
                source, target = raw_source, raw_target
            if isinstance(raw_kind, str):
                kind = raw_kind
        else:
            raise PolicyError("resolved worker volume is malformed")
        if target != _TARGET:
            continue
        if kind != "bind" or not source:
            raise PolicyError("/app/config must be a bind mount")
        candidate = Path(source)
        if not candidate.is_absolute():
            candidate = directory / candidate
        if candidate.is_symlink():
            raise PolicyError("resolved /app/config bind source is unsafe")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as error:
            raise PolicyError("resolved /app/config bind source is unavailable") from error
        if resolved.is_symlink() or not resolved.is_dir():
            raise PolicyError("resolved /app/config bind source is unsafe")
        roots.append(resolved)
    if len(roots) != 1:
        raise PolicyError("each worker requires exactly one /app/config bind mount")
    return roots[0]


def _container_path(environment: dict[str, str]) -> str:
    raw = environment.get("INGESTION_CONFIG_FILE")
    if not raw:
        raise PolicyError("INGESTION_CONFIG_FILE is missing")
    try:
        relative = PurePosixPath(raw).relative_to(_TARGET)
    except ValueError as error:
        raise PolicyError("INGESTION_CONFIG_FILE must stay inside /app/config") from error
    if str(relative) in {"", "."} or ".." in relative.parts:
        raise PolicyError("INGESTION_CONFIG_FILE must name a regular config file")
    return raw


def _read_json(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PolicyError("effective ingestion config is not valid JSON") from error
    return _mapping(raw, "effective ingestion config must be a JSON object")


def _time(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise PolicyError(f"scheduled_ingestion.{key} is invalid")
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as error:
        raise PolicyError(f"scheduled_ingestion.{key} is invalid") from error
    return value


def _policy(
    payload: dict[str, object],
    *,
    require_explicit: bool,
) -> tuple[bool, str, str, str, int, bool]:
    scheduled = _mapping(payload.get("scheduled_ingestion", {}), "scheduled_ingestion is invalid")
    enabled = scheduled.get("enabled", False)
    if not isinstance(enabled, bool):
        raise PolicyError("scheduled_ingestion.enabled is invalid")
    missing = tuple(key for key in _POLICY if key not in scheduled)
    if require_explicit and missing:
        raise PolicyError(f"scheduled_ingestion.{missing[0]} is required")
    timezone = scheduled.get("timezone", _POLICY["timezone"])
    if not isinstance(timezone, str):
        raise PolicyError("scheduled_ingestion.timezone is invalid")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise PolicyError("scheduled_ingestion.timezone is invalid") from error
    opening = _time(scheduled.get("local_opening", _POLICY["local_opening"]), "local_opening")
    cutoff = _time(scheduled.get("local_cutoff", _POLICY["local_cutoff"]), "local_cutoff")
    reserve = scheduled.get("drain_reserve_seconds", _POLICY["drain_reserve_seconds"])
    if isinstance(reserve, bool) or not isinstance(reserve, int) or reserve <= 0:
        raise PolicyError("scheduled_ingestion.drain_reserve_seconds is invalid")
    if opening >= cutoff:
        raise PolicyError("scheduled_ingestion opening must precede cutoff")
    if (timezone, opening, cutoff, reserve) != ("Asia/Singapore", "09:00", "23:00", 900):
        raise PolicyError("scheduled_ingestion policy must match the approved deployment window")
    return enabled, timezone, opening, cutoff, reserve, bool(missing)


def _is_legacy_bare_exclusions(payload: dict[str, object]) -> bool:
    return not _NESTED_CONFIG_SECTIONS.intersection(payload)


def resolve_effective_config(
    document: dict[str, object],
    directory: Path,
    *,
    require_explicit: bool,
) -> EffectiveConfig:
    """Resolve the shared host config that backs /app/config in Compose."""
    services = _mapping(document.get("services"), "resolved Compose document has no services")
    roots: list[Path] = []
    container_paths: list[str] = []
    for name in _SERVICES:
        service = _mapping(services.get(name), "resolved Compose worker is missing")
        environment = _environment(service)
        for key, expected in _POLICY_ENV.items():
            if environment.get(key) != expected:
                raise PolicyError("workers disagree with the approved Compose deployment policy")
        roots.append(_bind_root(service, directory))
        container_paths.append(_container_path(environment))
    if len(set(roots)) != 1 or len(set(container_paths)) != 1:
        raise PolicyError("workers disagree about effective /app/config ingestion configuration")
    root = roots[0]
    relative = PurePosixPath(container_paths[0]).relative_to(_TARGET)
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink():
        raise PolicyError("INGESTION_CONFIG_FILE must be a regular file")
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as error:
        raise PolicyError("INGESTION_CONFIG_FILE escapes the /app/config bind") from error
    if path.is_symlink() or not path.is_file():
        raise PolicyError("INGESTION_CONFIG_FILE must be a regular file")
    payload = _read_json(path)
    legacy_bare_exclusions = _is_legacy_bare_exclusions(payload)
    enabled, timezone, opening, cutoff, reserve, needs_migration = _policy(
        payload,
        require_explicit=require_explicit,
    )
    return EffectiveConfig(
        path,
        enabled,
        timezone,
        opening,
        cutoff,
        reserve,
        needs_migration or legacy_bare_exclusions,
        legacy_bare_exclusions,
        payload,
    )


def _bytes(value: object) -> int:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise PolicyError("resolved worker memory limit is invalid")
    text = str(value).upper().strip()
    units = {"GB": 1024**3, "G": 1024**3, "MB": 1024**2, "M": 1024**2}
    for suffix, multiplier in units.items():
        if text.endswith(suffix):
            try:
                return int(float(text[: -len(suffix)]) * multiplier)
            except ValueError as error:
                raise PolicyError("resolved worker memory limit is invalid") from error
    try:
        result = int(float(text))
    except ValueError as error:
        raise PolicyError("resolved worker memory limit is invalid") from error
    if result <= 0:
        raise PolicyError("resolved worker memory limit is invalid")
    return result


def _seconds(value: object) -> float:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise PolicyError("resolved worker stop grace is invalid")
    text = str(value).lower().strip()
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        result = float(text)
    else:
        components = list(re.finditer(r"(\d+(?:\.\d+)?)([hms])", text))
        if not components or "".join(match.group(0) for match in components) != text:
            raise PolicyError("resolved worker stop grace is invalid")
        multipliers = {"h": 3600.0, "m": 60.0, "s": 1.0}
        order = {"h": 0, "m": 1, "s": 2}
        previous = -1
        result = 0.0
        for component in components:
            unit = component.group(2)
            current = order[unit]
            if current <= previous:
                raise PolicyError("resolved worker stop grace is invalid")
            previous = current
            result += float(component.group(1)) * multipliers[unit]
    if result <= 0:
        raise PolicyError("resolved worker stop grace is invalid")
    return result


def _concurrency(service: dict[str, object]) -> None:
    command = service.get("command")
    if not isinstance(command, list):
        raise PolicyError("resolved worker command must set Celery concurrency")
    values = [
        value.removeprefix("--concurrency=")
        for value in command
        if isinstance(value, str) and value.startswith("--concurrency=")
    ]
    if len(values) != 1 or not values[0].isdigit():
        raise PolicyError("resolved worker concurrency is invalid")
    if not 0 < int(values[0]) <= 1:
        raise PolicyError("resolved worker concurrency exceeds its deployment ceiling")


def _validate_resources(document: dict[str, object]) -> None:
    services = _mapping(document.get("services"), "resolved Compose document has no services")
    for name, (cpu_limit, memory_limit, grace_limit) in _CEILINGS.items():
        service = _mapping(services.get(name), "resolved Compose worker is missing")
        try:
            cpus = float(cast(object, service.get("cpus")))
        except (TypeError, ValueError) as error:
            raise PolicyError("resolved worker CPU limit is invalid") from error
        if (
            not isfinite(cpus)
            or cpus <= 0
            or cpus > cpu_limit
            or _bytes(service.get("mem_limit")) > memory_limit
        ):
            raise PolicyError("resolved worker resource limit exceeds its ceiling")
        if _seconds(service.get("stop_grace_period")) >= float(grace_limit):
            raise PolicyError("resolved worker stop grace must remain below drain reserve")
        if name != "beat":
            _concurrency(service)


def _document(args: argparse.Namespace) -> tuple[dict[str, object], Path]:
    if args.compose_json:
        try:
            raw = json.loads(Path(args.compose_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PolicyError("resolved Compose JSON is invalid") from error
        directory = Path(args.compose_directory).resolve()
        return _mapping(raw, "resolved Compose JSON must be an object"), directory
    compose_file = Path(args.compose_file).resolve()
    environment = dict(os.environ)
    environment["COMPOSE_PROFILES"] = ""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            args.compose_project,
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        raise PolicyError("could not resolve staging Compose configuration")
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise PolicyError("Docker Compose did not produce JSON configuration") from error
    return _mapping(raw, "resolved Compose JSON must be an object"), compose_file.parent


def _shell(name: str, value: str | int | bool) -> None:
    rendered = "true" if value is True else "false" if value is False else str(value)
    print(f"{name}={shlex.quote(rendered)}")


def _emit(config: EffectiveConfig) -> None:
    _shell("SCHEDULE_POLICY_ENABLED", config.enabled)
    _shell("SCHEDULE_POLICY_TIMEZONE", config.timezone)
    _shell("SCHEDULE_POLICY_LOCAL_OPENING", config.opening)
    _shell("SCHEDULE_POLICY_LOCAL_CUTOFF", config.cutoff)
    _shell("SCHEDULE_POLICY_DRAIN_RESERVE_SECONDS", config.reserve_seconds)
    _shell("SCHEDULE_POLICY_NEEDS_MIGRATION", config.needs_migration)


def _assert_config_is_untracked(config_path: Path, repository_root: Path) -> None:
    if repository_root.is_symlink() or not repository_root.is_dir():
        raise PolicyError("repository root for config migration is unsafe")
    try:
        relative = config_path.relative_to(repository_root.resolve(strict=True))
    except (OSError, ValueError):
        return
    result = subprocess.run(
        ["git", "-C", str(repository_root), "ls-files", "--error-unmatch", "--", str(relative)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        raise PolicyError("effective ingestion config is tracked; refusing to dirty checkout")
    if result.returncode != 1:
        raise PolicyError("could not inspect effective ingestion config tracking")


def _atomic_replace(path: Path, payload: dict[str, object]) -> None:
    source_metadata = path.stat()
    mode = stat.S_IMODE(source_metadata.st_mode)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    serialized = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        remaining = memoryview(serialized)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while preparing effective ingestion config")
            remaining = remaining[written:]
        os.fsync(descriptor)
        if not hasattr(os, "fchown"):
            raise OSError("fchown is unavailable for ownership preservation")
        os.fchown(descriptor, source_metadata.st_uid, source_metadata.st_gid)
        os.fchmod(descriptor, mode)
        temporary_metadata = os.fstat(descriptor)
        if (
            temporary_metadata.st_uid != source_metadata.st_uid
            or temporary_metadata.st_gid != source_metadata.st_gid
            or stat.S_IMODE(temporary_metadata.st_mode) != mode
        ):
            raise OSError("temporary config metadata did not match source metadata")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise PolicyError("could not atomically prepare effective ingestion config") from error


def _prepare(config: EffectiveConfig, repository_root: Path) -> EffectiveConfig:
    if not config.needs_migration:
        strict_policy = _policy(config.payload, require_explicit=True)
        return EffectiveConfig(
            config.path,
            *strict_policy,
            False,
            config.payload,
        )
    _assert_config_is_untracked(config.path, repository_root)
    if config.legacy_bare_exclusions:
        payload = {"exclusions": dict(config.payload)}
    else:
        payload = dict(config.payload)
    scheduled = dict(
        _mapping(payload.get("scheduled_ingestion", {}), "scheduled_ingestion is invalid")
    )
    for key, value in _POLICY.items():
        scheduled.setdefault(key, value)
    payload["scheduled_ingestion"] = scheduled
    _atomic_replace(config.path, payload)
    persisted = _read_json(config.path)
    enabled, timezone, opening, cutoff, reserve, needs_migration = _policy(
        persisted,
        require_explicit=True,
    )
    if needs_migration:
        raise PolicyError("effective ingestion config migration did not read back")
    return EffectiveConfig(
        config.path,
        enabled,
        timezone,
        opening,
        cutoff,
        reserve,
        False,
        False,
        persisted,
    )


def _admitted(config: EffectiveConfig, now: datetime) -> bool:
    local = now.astimezone(ZoneInfo(config.timezone))
    opening = datetime.strptime(config.opening, "%H:%M").time()
    cutoff = datetime.strptime(config.cutoff, "%H:%M").time()
    current = local.hour * 3600 + local.minute * 60 + local.second
    start = opening.hour * 3600 + opening.minute * 60
    end = cutoff.hour * 3600 + cutoff.minute * 60 - config.reserve_seconds
    return start <= current < end


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("probe", "inspect", "prepare", "admit"):
        command = commands.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--compose-json")
        source.add_argument("--compose-file")
        command.add_argument("--compose-directory", default=".")
        command.add_argument("--compose-project", default="hyperp-ada-asia")
        if name == "prepare":
            command.add_argument("--repository-root", required=True)
        if name == "admit":
            command.add_argument("--now")
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        document, directory = _document(args)
        _validate_resources(document)
        require_explicit = args.command == "inspect"
        config = resolve_effective_config(
            document,
            directory,
            require_explicit=require_explicit,
        )
        if args.command in {"probe", "inspect"}:
            _emit(config)
            return 0
        if args.command == "prepare":
            prepared = _prepare(config, Path(args.repository_root))
            _emit(prepared)
            return 0
        now = (
            datetime.fromisoformat(args.now)
            if args.now
            else datetime.now(tz=ZoneInfo(config.timezone))
        )
        if now.tzinfo is None:
            raise PolicyError("admission clock must be timezone-aware")
        if _admitted(config, now):
            return 0
        print("scheduled-ingestion deployment admission is closed", file=sys.stderr)
        return 1
    except PolicyError as error:
        print(f"scheduled-ingestion policy error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
