"""Explicit reviewed command registration; no discovery or shell execution."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts_manifest import RUNTIME_LIMIT_KEYS

type Cancelled = Callable[[], bool]
type CommandHandler = Callable[[Path, Cancelled], None]
PublicMetadataValue = str | int | float | bool | None
ChildLimitName = str
ChildLimits = Mapping[ChildLimitName, int]
RuntimeLimits = Mapping[str, int]
_CHILD_LIMIT_KEYS = frozenset({"max_cpu_seconds", "max_address_space_bytes"})
_MAX_PUBLIC_METADATA_ITEMS = 32
_MAX_PUBLIC_METADATA_VALUE_LENGTH = 512


def validate_public_metadata(
    value: Mapping[str, PublicMetadataValue],
) -> dict[str, PublicMetadataValue]:
    """Return one bounded, secret-free scalar provenance mapping."""
    if len(value) > _MAX_PUBLIC_METADATA_ITEMS:
        raise ValueError("command public metadata has too many fields")
    result: dict[str, PublicMetadataValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ValueError("command public metadata must be safe")
        if any(
            marker in key.lower()
            for marker in ("secret", "token", "password", "credential", "authorization")
        ):
            raise ValueError("command public metadata must be safe")
        if not isinstance(item, (str, int, float, bool)) and item is not None:
            raise ValueError("command public metadata value is invalid")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("command public metadata value is invalid")
        if isinstance(item, str) and (not item or len(item) > _MAX_PUBLIC_METADATA_VALUE_LENGTH):
            raise ValueError("command public metadata value is invalid")
        result[key] = item
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class RegisteredCommand:
    """A repository-reviewed callable, never a caller-provided executable."""

    name: str
    mutates: bool
    execute: CommandHandler
    public_metadata: Mapping[str, PublicMetadataValue]
    child_limits: ChildLimits | None = None
    runtime_limits: RuntimeLimits | None = None

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or self.name.startswith("_"):
            raise ValueError("command name must be a public Python identifier")
        validate_public_metadata(self.public_metadata)
        if self.child_limits is not None and (
            set(self.child_limits) != _CHILD_LIMIT_KEYS
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in self.child_limits.values()
            )
        ):
            raise ValueError("command child limits are invalid")
        if self.runtime_limits is not None and (
            set(self.runtime_limits) != RUNTIME_LIMIT_KEYS
            or any(
                not isinstance(value, int) or isinstance(value, bool) or value < 1
                for value in self.runtime_limits.values()
            )
        ):
            raise ValueError("command runtime limits are invalid")


class Registry:
    """Immutable command registry with intentional empty production default."""

    def __init__(self, commands: tuple[RegisteredCommand, ...] = ()) -> None:
        names = tuple(command.name for command in commands)
        if len(names) != len(set(names)):
            raise ValueError("duplicate command name")
        self._commands = {command.name: command for command in commands}

    def get(self, name: str) -> RegisteredCommand:
        """Return one exact allowlisted command."""
        try:
            return self._commands[name]
        except KeyError as error:
            raise ValueError(f"unknown command: {name}") from error

    def names(self) -> tuple[str, ...]:
        """Return deterministic registered names."""
        return tuple(sorted(self._commands))


PRODUCTION_REGISTRY = Registry()
