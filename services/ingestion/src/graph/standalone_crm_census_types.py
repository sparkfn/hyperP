"""Public errors and result records for standalone CRM census persistence."""

from __future__ import annotations

from dataclasses import dataclass

from src.standalone_crm_census_models import (
    StandaloneCrmFreshness,
    StandaloneCrmPublicationState,
)


class StandaloneCrmCensusConflictError(RuntimeError):
    """An immutable occurrence, scope owner, or publication identity conflicts."""


class StandaloneCrmCensusStaleError(RuntimeError):
    """A durable source/control authority, generation, fence, or deadline is stale."""


@dataclass(frozen=True)
class StandaloneCrmCensusAdmission:
    census_id: str
    state: str
    fingerprint: str
    authority_digest: str
    source_instance_id: str
    control_instance_id: str
    created: bool

    @property
    def freshness(self) -> StandaloneCrmFreshness:
        return StandaloneCrmFreshness(
            self.census_id,
            self.fingerprint,
            self.authority_digest,
            self.source_instance_id,
            self.control_instance_id,
        )


@dataclass(frozen=True)
class StandaloneCrmPublication:
    publication_id: str
    task_id: str
    payload_json: str
    payload_digest: str
    task_name: str
    queue: str
    status: StandaloneCrmPublicationState


@dataclass(frozen=True)
class StandaloneCrmCensusStatus:
    census: dict[str, object]
    attempts: tuple[dict[str, object], ...]
    units: tuple[dict[str, object], ...]
    publications: tuple[dict[str, object], ...]
    fences: tuple[dict[str, object], ...]
