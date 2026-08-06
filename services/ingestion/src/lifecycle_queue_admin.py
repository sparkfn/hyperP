"""Owner-safe inspection and recovery for lifecycle Redis queue gates."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Literal, cast

import redis

from src.config import get_settings
from src.pipeline_knows import KnowsMaterializationPhase

_RECONCILIATION_GATE_KEY = "profile_unifier:lifecycle-reconciliation:queued"
_KNOWS_GATE_PREFIX = "profile_unifier:knows-materialization"
_OWNER_CLEAR_SCRIPT = """
local current = redis.call('get', KEYS[1])
if not current then
    return 0
end
local state, timestamp, owner = string.match(current, '^([^|]+)|([^|]+)|(.+)$')
if current == ARGV[1] or owner == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class _Command:
    name: Literal["status", "clear-knows", "clear-reconciliation"]
    phase: KnowsMaterializationPhase | None = None
    expected_owner: str | None = None


@dataclass(frozen=True)
class _GateStatus:
    state: Literal["absent", "queued", "publishing", "malformed"]
    owner: str | None
    publishing_at: int | None = None


def _redis_client() -> redis.Redis:
    return redis.Redis.from_url(get_settings().celery_broker_url)


def _phase(value: str) -> KnowsMaterializationPhase:
    if value not in {"contacts", "chat_relationships"}:
        raise argparse.ArgumentTypeError("phase must be contacts or chat_relationships")
    return cast(KnowsMaterializationPhase, value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect or owner-clear lifecycle queue gates.")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status")
    clear_knows = subcommands.add_parser("clear-knows")
    clear_knows.add_argument("--phase", required=True, type=_phase)
    clear_knows.add_argument("--expected-owner", required=True)
    clear_reconciliation = subcommands.add_parser("clear-reconciliation")
    clear_reconciliation.add_argument("--expected-owner", required=True)
    return parser


def _parse_command(argv: list[str] | None) -> _Command:
    args = _build_parser().parse_args(argv)
    command = cast(str, args.command)
    if command == "status":
        return _Command(name="status")
    expected_owner = cast(str, args.expected_owner)
    if command == "clear-knows":
        return _Command(
            name="clear-knows",
            phase=cast(KnowsMaterializationPhase, args.phase),
            expected_owner=expected_owner,
        )
    return _Command(name="clear-reconciliation", expected_owner=expected_owner)


def _decode(value: object) -> str | None:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value if isinstance(value, str) else None


def _knows_gate_key(phase: KnowsMaterializationPhase) -> str:
    return f"{_KNOWS_GATE_PREFIX}:{phase}:queued"


def _gate_status(client: redis.Redis, key: str) -> _GateStatus:
    value = _decode(client.get(key))
    if value is None:
        return _GateStatus(state="absent", owner=None)
    parts = value.split("|", 2)
    if len(parts) == 1:
        return _GateStatus(state="queued", owner=value)
    if len(parts) == 3 and parts[0] == "publishing" and parts[2]:
        try:
            publishing_at = int(parts[1])
        except ValueError:
            return _GateStatus(state="malformed", owner=None)
        return _GateStatus(
            state="publishing",
            owner=parts[2],
            publishing_at=publishing_at,
        )
    return _GateStatus(state="malformed", owner=None)


def _owner_clear(client: redis.Redis, key: str, expected_owner: str) -> bool:
    cleared = cast(int, client.eval(_OWNER_CLEAR_SCRIPT, 1, key, expected_owner))
    return cleared == 1


def _print_gate_status(label: str, status: _GateStatus) -> None:
    print(f"{label}_state={status.state}")
    print(f"{label}_owner={status.owner or 'none'}")
    if status.publishing_at is not None:
        print(f"{label}_publishing_at={status.publishing_at}")


def _print_status(client: redis.Redis) -> None:
    _print_gate_status("reconciliation", _gate_status(client, _RECONCILIATION_GATE_KEY))
    for phase in ("contacts", "chat_relationships"):
        _print_gate_status(f"knows_{phase}", _gate_status(client, _knows_gate_key(phase)))


def main(argv: list[str] | None = None) -> int:
    command = _parse_command(argv)
    try:
        client = _redis_client()
        if command.name == "status":
            _print_status(client)
            return 0
        if command.expected_owner is None:
            raise RuntimeError("clear command is missing its expected owner")
        key = _RECONCILIATION_GATE_KEY
        if command.name == "clear-knows":
            if command.phase is None:
                raise RuntimeError("clear-knows command is missing its phase")
            key = _knows_gate_key(command.phase)
        if not _owner_clear(client, key, command.expected_owner):
            print("gate was not cleared: owner did not match", file=sys.stderr)
            return 1
    except redis.RedisError as exc:
        print(f"queue gate backend error: {type(exc).__name__}", file=sys.stderr)
        return 2
    print("gate cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
