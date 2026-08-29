"""Tests for immutable empty component registry and isolated reserved lanes."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module

import pytest
from deal_intelligence.platform.extensions import (
    ScheduleDescriptor,
    TaskDescriptor,
    build_component_registry,
)
from deal_intelligence.platform.types import ComponentDescriptor

LANES = (
    "identity",
    "deal_stage",
    "activity",
    "historical_import",
    "artifact",
    "projection_outbox",
    "ownership",
)


@dataclass(frozen=True, slots=True)
class FakeReservedComponent:
    descriptor: ComponentDescriptor


@pytest.mark.parametrize("lane", LANES)
def test_reserved_lane_is_empty_and_fake_isolated(lane: str) -> None:
    module = import_module(f"deal_intelligence.components.{lane}")
    fake = FakeReservedComponent(ComponentDescriptor(name=lane, branch_label=lane))
    assert module.COMPONENT == fake.descriptor
    assert module.PLUGINS == ()
    assert module.TASKS == ()
    assert module.SCHEDULES == ()


def test_registry_defaults_are_empty_immutable_and_disabled() -> None:
    registry = build_component_registry()
    assert registry.components == ()
    assert registry.tasks == ()
    assert registry.schedules == ()


def test_registry_rejects_enabled_component() -> None:
    with pytest.raises(ValueError):
        build_component_registry(components=(ComponentDescriptor("a", "a", enabled=True),))


def test_registry_rejects_enabled_task() -> None:
    with pytest.raises(ValueError):
        build_component_registry(tasks=(TaskDescriptor("a", enabled=True),))


def test_registry_rejects_enabled_schedule() -> None:
    with pytest.raises(ValueError):
        build_component_registry(schedules=(ScheduleDescriptor("a", enabled=True),))


def test_registry_rejects_duplicate_descriptors() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        build_component_registry(tasks=(TaskDescriptor("same"), TaskDescriptor("same")))
