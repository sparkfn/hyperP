"""Dependency-light types shared by current and future platform components."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from re import Pattern
from re import compile as re_compile
from uuid import UUID

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

SOURCE_SYSTEM_MAX_LENGTH = 80
INSTANCE_KEY_MAX_LENGTH = 255
DISPLAY_NAME_MAX_LENGTH = 255
_SLUG_PATTERN: Pattern[str] = re_compile(r"^[a-z][a-z0-9]*([_-][a-z0-9]+)*$")


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class UnitStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ComponentDescriptor:
    """A reserved component namespace with no enabled implementation."""

    name: str
    branch_label: str
    dependency_labels: tuple[str, ...] = ("platform",)
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class SourceInstanceRegistration:
    source_system: str
    instance_key: str
    display_name: str
    is_enabled: bool = False

    def __post_init__(self) -> None:
        """Reject credentials, URLs, and ambiguous source identifiers at the boundary."""
        _validate_slug("source_system", self.source_system, SOURCE_SYSTEM_MAX_LENGTH)
        _validate_slug("instance_key", self.instance_key, INSTANCE_KEY_MAX_LENGTH)
        _validate_display_name(self.display_name)


@dataclass(frozen=True, slots=True)
class SourceInstanceRecord:
    id: UUID
    registration: SourceInstanceRegistration
    registered_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RunDescriptor:
    component_name: str
    run_kind: str
    source_instance_id: UUID | None
    requested_by: str | None


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: UUID
    descriptor: RunDescriptor
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    terminal_disposition: str | None


@dataclass(frozen=True, slots=True)
class UnitDescriptor:
    run_id: UUID
    unit_key: str
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class Checkpoint:
    run_id: UUID
    checkpoint_key: str
    version: int
    payload: JsonValue
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CompareAndSet:
    expected_version: int
    payload: JsonValue


@dataclass(frozen=True, slots=True)
class CompareAndSetResult:
    applied: bool
    checkpoint: Checkpoint | None


@dataclass(frozen=True, slots=True)
class Lease:
    resource_key: str
    owner_run_id: UUID
    fence_token: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TerminalAccounting:
    run_id: UUID
    terminal_disposition: str
    succeeded_count: int
    failed_count: int
    skipped_count: int
    total_count: int
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ProcessHeartbeat:
    component: str
    heartbeat_at: datetime
    details: JsonValue


@dataclass(frozen=True, slots=True)
class SchemaReadiness:
    component: str
    is_ready: bool
    expected_revisions: tuple[str, ...]
    observed_revisions: tuple[str, ...]
    checked_at: datetime
    details: JsonValue


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    status: str
    component: str
    writers_enabled: bool = False
    task_count: int = 0
    schedule_count: int = 0

    def as_dict(self) -> dict[str, str | bool | int]:
        return {
            "status": self.status,
            "component": self.component,
            "writers_enabled": self.writers_enabled,
            "task_count": self.task_count,
            "schedule_count": self.schedule_count,
        }


def _validate_slug(field_name: str, value: str, maximum_length: int) -> None:
    if len(value) > maximum_length or _SLUG_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field_name} must be a lowercase slug no longer than {maximum_length} characters"
        )


def _validate_display_name(value: str) -> None:
    if len(value) > DISPLAY_NAME_MAX_LENGTH or value != value.strip() or not value:
        raise ValueError(
            f"display_name must be nonempty and no longer than {DISPLAY_NAME_MAX_LENGTH}"
        )
    if "://" in value or "@" in value:
        raise ValueError("display_name must not contain a URL or credential-shaped value")
