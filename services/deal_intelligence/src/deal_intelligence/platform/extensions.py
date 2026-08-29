"""Immutable extension registry seams for a disabled Deal Intelligence platform."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from deal_intelligence.platform.types import ComponentDescriptor


class ComponentPlugin(Protocol):
    @property
    def descriptor(self) -> ComponentDescriptor:
        """Return the component namespace this plugin belongs to."""


@dataclass(frozen=True, slots=True)
class TaskDescriptor:
    name: str
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ScheduleDescriptor:
    name: str
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ComponentRegistry:
    """A fixed registry that intentionally exposes no executable work by default."""

    components: tuple[ComponentDescriptor, ...] = ()
    tasks: tuple[TaskDescriptor, ...] = ()
    schedules: tuple[ScheduleDescriptor, ...] = ()

    def __post_init__(self) -> None:
        _validate_descriptors(self.components, "component")
        _validate_descriptors(self.tasks, "task")
        _validate_descriptors(self.schedules, "schedule")


class ComponentRegistryProvider(Protocol):
    """Supplies the immutable descriptor set without loading writers."""

    def registry(self) -> ComponentRegistry:
        """Return immutable disabled descriptors."""


@dataclass(frozen=True, slots=True)
class StaticComponentRegistryProvider:
    """A testable provider for an already validated registry."""

    value: ComponentRegistry = field(default_factory=ComponentRegistry)

    def registry(self) -> ComponentRegistry:
        return self.value


def build_component_registry(
    components: tuple[ComponentDescriptor, ...] = (),
    tasks: tuple[TaskDescriptor, ...] = (),
    schedules: tuple[ScheduleDescriptor, ...] = (),
) -> ComponentRegistry:
    """Build a validated immutable registry; enabled registrations are forbidden."""
    return ComponentRegistry(components=components, tasks=tasks, schedules=schedules)


def _validate_descriptors(
    descriptors: tuple[ComponentDescriptor, ...]
    | tuple[TaskDescriptor, ...]
    | tuple[ScheduleDescriptor, ...],
    kind: str,
) -> None:
    names = tuple(descriptor.name for descriptor in descriptors)
    if len(names) != len(set(names)):
        raise ValueError(f"Duplicate {kind} descriptor names are not allowed")
    if any(descriptor.enabled for descriptor in descriptors):
        raise ValueError(f"Enabled {kind} registrations are not allowed")
