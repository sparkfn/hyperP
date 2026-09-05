"""Validated, bounded, secret-free runtime configuration."""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path


def _positive(name: str, default: int) -> int:
    value = environ.get(name, str(default))
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed < 1:
        raise ValueError(f"{name} must be positive")
    return parsed


def _boolean(name: str, default: bool) -> bool:
    value = environ.get(name, str(default).lower()).lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class RuntimeConfig:
    """Configuration that may influence runtime behavior but is never persisted."""

    workspace: Path
    mutations_enabled: bool = False
    max_runtime_seconds: int = 3_600
    max_log_bytes: int = 1_000_000
    max_output_bytes: int = 100_000_000
    stale_seconds: int = 300

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        """Build validated configuration from the limited Intelligence environment."""
        workspace = Path(environ.get("INTELLIGENCE_WORKSPACE", "/var/lib/intelligence"))
        return cls(
            workspace=workspace,
            mutations_enabled=_boolean("INTELLIGENCE_MUTATIONS_ENABLED", False),
            max_runtime_seconds=_positive("INTELLIGENCE_MAX_RUNTIME_SECONDS", 3_600),
            max_log_bytes=_positive("INTELLIGENCE_MAX_LOG_BYTES", 1_000_000),
            max_output_bytes=_positive("INTELLIGENCE_MAX_OUTPUT_BYTES", 100_000_000),
            stale_seconds=_positive("INTELLIGENCE_STALE_SECONDS", 300),
        )
