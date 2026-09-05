"""Explicit reviewed command registration; no discovery or shell execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

type Cancelled = Callable[[], bool]
type CommandHandler = Callable[[Path, Cancelled], None]
PublicMetadataValue = str | int | float | bool | None


@dataclass(frozen=True)
class RegisteredCommand:
    """A repository-reviewed callable, never a caller-provided executable."""

    name: str
    mutates: bool
    execute: CommandHandler
    public_metadata: Mapping[str, PublicMetadataValue]

    def __post_init__(self) -> None:
        if not self.name.isidentifier() or self.name.startswith("_"):
            raise ValueError("command name must be a public Python identifier")
        if any("secret" in key.lower() or "token" in key.lower() for key in self.public_metadata):
            raise ValueError("command public metadata must not contain secret-like keys")


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
