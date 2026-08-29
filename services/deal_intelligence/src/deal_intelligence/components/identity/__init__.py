"""Reserved identity extension lane with no writers, tasks, or schedules."""

from deal_intelligence.platform.extensions import (
    ComponentPlugin,
    ScheduleDescriptor,
    TaskDescriptor,
)
from deal_intelligence.platform.types import ComponentDescriptor

COMPONENT = ComponentDescriptor(name="identity", branch_label="identity")
PLUGINS: tuple[ComponentPlugin, ...] = ()
TASKS: tuple[TaskDescriptor, ...] = ()
SCHEDULES: tuple[ScheduleDescriptor, ...] = ()
