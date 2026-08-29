"""Reserved deal_stage extension lane with no writers, tasks, or schedules."""

from deal_intelligence.platform.extensions import (
    ComponentPlugin,
    ScheduleDescriptor,
    TaskDescriptor,
)
from deal_intelligence.platform.types import ComponentDescriptor

COMPONENT = ComponentDescriptor(name="deal_stage", branch_label="deal_stage")
PLUGINS: tuple[ComponentPlugin, ...] = ()
TASKS: tuple[TaskDescriptor, ...] = ()
SCHEDULES: tuple[ScheduleDescriptor, ...] = ()
