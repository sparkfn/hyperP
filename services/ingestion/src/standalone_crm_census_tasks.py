"""Default-off Celery entry points for standalone CRM census control."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from src.celery_app import celery_app
from src.config import get_settings
from src.graph.bitrix_source_instances import BitrixSourceInstanceRepository
from src.graph.client import Neo4jClient
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_models import (
    MappingAuthorityUnavailableError,
    ParentState,
)
from src.standalone_crm_census_runtime import (
    MappingAuthorityReader,
    StandaloneCrmCensusRuntime,
)


class _UnavailableCensusAuthority:
    """Fail closed until #275 supplies the authoritative mapping/projection reader."""

    def prepare_head(self, request: object) -> None:
        raise MappingAuthorityUnavailableError("mapping prepare authority is unavailable")

    def rollback_head(self, request: object) -> None:
        raise MappingAuthorityUnavailableError("mapping rollback authority is unavailable")

    def source_heads(self) -> None:
        raise MappingAuthorityUnavailableError("source authority is unavailable")

    def probe_upper_id(self, kind: str) -> int:
        raise MappingAuthorityUnavailableError("source probe authority is unavailable")


def _with_runtime[T](action: Callable[[StandaloneCrmCensusRuntime], T]) -> T:
    client = Neo4jClient(get_settings())
    try:
        return action(
            StandaloneCrmCensusRuntime(
                source_repository=BitrixSourceInstanceRepository(client),
                census_repository=StandaloneCrmCensusRepository(client),
                authority=cast(MappingAuthorityReader, _UnavailableCensusAuthority()),
                clock=lambda: datetime.now(UTC),
            )
        )
    finally:
        client.close()


@celery_app.task(name="src.standalone_crm_census_tasks.pause_census_task")
def pause_census_task(census_id: str, fingerprint: str, reason: str) -> None:
    _with_runtime(
        lambda runtime: runtime.pause(census_id=census_id, fingerprint=fingerprint, reason=reason)
    )


@celery_app.task(name="src.standalone_crm_census_tasks.resume_census_task")
def resume_census_task(census_id: str, fingerprint: str) -> None:
    _with_runtime(
        lambda runtime: runtime.continue_census(census_id=census_id, fingerprint=fingerprint)
    )


@celery_app.task(name="src.standalone_crm_census_tasks.cancel_census_task")
def cancel_census_task(census_id: str, fingerprint: str, actor: str) -> str:
    return _with_runtime(
        lambda runtime: (
            runtime.cancel(census_id=census_id, fingerprint=fingerprint, actor=actor).value
        )
    )


@celery_app.task(name="src.standalone_crm_census_tasks.finalize_census_task")
def finalize_census_task(
    census_id: str,
    fingerprint: str,
    terminal_state: str,
    reason: str,
    allow_paused: bool = False,
) -> None:
    def _finalize(runtime: StandaloneCrmCensusRuntime) -> None:
        runtime.finalize(
            census_id=census_id,
            fingerprint=fingerprint,
            terminal_state=ParentState(terminal_state),
            reason=reason,
            allow_paused=allow_paused,
        )

    _with_runtime(_finalize)
