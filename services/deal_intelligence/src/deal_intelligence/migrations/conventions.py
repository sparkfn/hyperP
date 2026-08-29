"""Reserved Alembic lanes and cross-component ordering conventions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MigrationLane:
    name: str
    branch_label: str
    directory_name: str
    depends_on: tuple[str, ...]


MIGRATION_LANES: tuple[MigrationLane, ...] = (
    MigrationLane("baseline", "baseline", "baseline", ()),
    MigrationLane("platform", "platform", "platform", ("baseline",)),
    MigrationLane("identity", "identity", "identity", ("platform",)),
    MigrationLane("deal_stage", "deal_stage", "deal_stage", ("platform",)),
    MigrationLane("activity", "activity", "activity", ("platform",)),
    MigrationLane("historical_import", "historical_import", "historical_import", ("platform",)),
    MigrationLane("artifact", "artifact", "artifact", ("platform",)),
    MigrationLane("projection_outbox", "projection_outbox", "projection_outbox", ("platform",)),
    MigrationLane("ownership", "ownership", "ownership", ("platform",)),
)


def lane_for_branch_label(branch_label: str) -> MigrationLane:
    for lane in MIGRATION_LANES:
        if lane.branch_label == branch_label:
            return lane
    raise ValueError(f"Unknown Deal Intelligence migration branch label: {branch_label}")
