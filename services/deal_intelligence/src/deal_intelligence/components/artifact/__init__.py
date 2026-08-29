"""Reserved artifact extension lane with no writers, tasks, or schedules."""

from deal_intelligence.platform.extensions import (
    ComponentPlugin,
    ScheduleDescriptor,
    TaskDescriptor,
)
from deal_intelligence.platform.types import ComponentDescriptor

COMPONENT = ComponentDescriptor(name="artifact", branch_label="artifact")
PLUGINS: tuple[ComponentPlugin, ...] = ()
TASKS: tuple[TaskDescriptor, ...] = ()
SCHEDULES: tuple[ScheduleDescriptor, ...] = ()
