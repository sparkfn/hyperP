"""Durable publication and recovery helpers for the standalone CRM census runtime."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal, Protocol

from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.graph.standalone_crm_census_types import (
    StandaloneCrmCensusAdmission,
    StandaloneCrmCensusStatus,
    StandaloneCrmPublication,
)
from src.standalone_crm_census_models import (
    FrozenSourceWindow,
    StandaloneCrmAttempt,
    StandaloneCrmChildEnvelope,
)
from src.standalone_crm_census_requests import (
    MappingPrepareCensusRequest,
    MappingRollbackCensusRequest,
    SourceSyncAuthoritySnapshot,
    SourceSyncCensusRequest,
    StandaloneCrmCensusRequest,
)
from src.standalone_crm_census_runtime_envelopes import (
    canonical_json,
    mapping_envelope,
    source_envelope,
)
from src.standalone_crm_census_runtime_state import (
    attempt_from_status,
    int_field,
    payload_unit_kind,
    publication_generation,
    text_field,
)


class ChildPublisher(Protocol):
    """The only broker boundary used by standalone census publication."""

    def handler_available(self, census_kind: str, unit_kind: str) -> bool: ...

    def publish(self, *, task_name: str, task_id: str, queue: str, payload_json: str) -> None: ...


@dataclass(frozen=True)
class PublicationRunOutcome:
    """Publication work completed by one parent runtime invocation."""

    state: str
    published_children: int
    no_work_children: int


Revalidator = Callable[[StandaloneCrmCensusRequest, SourceSyncAuthoritySnapshot | None], None]


def unit_is_terminal(status: StandaloneCrmCensusStatus | None, unit_kind: str) -> bool:
    if status is None:
        return False
    for unit in status.units:
        if unit.get("unit_kind") == unit_kind:
            return unit.get("state") in {"completed", "failed", "cancelled", "superseded"}
    return False


def repair_publication(
    repository: StandaloneCrmCensusRepository,
    publisher: ChildPublisher,
    publication_id: str,
    revalidate: Revalidator,
) -> None:
    """Confirm observed work or republish the stored immutable outbox record."""
    admission, publication, observed = repository.publication_recovery(publication_id)
    request_admission, request, authority = repository.load_admitted_request(admission.census_id)
    if request_admission.freshness != admission.freshness:
        raise RuntimeError("publication admission changed")
    revalidate(request, authority)
    status = repository.status(admission.census_id)
    if status is None:
        raise RuntimeError("publication census is missing")
    attempt = _publication_attempt(status, publication)
    if observed != "none":
        repository.confirm_observed_publication(admission, attempt, publication.publication_id)
        return
    census_kind = text_field(status.census, "census_kind")
    _republish_reserved(
        repository,
        publisher,
        admission,
        attempt,
        publication,
        census_kind,
        lambda: revalidate(request, authority),
    )


def run_mapping_only(
    repository: StandaloneCrmCensusRepository,
    publisher: ChildPublisher,
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    request: MappingPrepareCensusRequest | MappingRollbackCensusRequest,
    revalidate: Revalidator,
) -> PublicationRunOutcome:
    revalidate(request, None)
    status = repository.status(admitted.census_id)
    unit_kind, revision_id = _mapping_identity(request)
    if status is None or status.census.get("no_source_window") is not True:
        repository.freeze_no_source_window(
            admitted, attempt, unit_kind=unit_kind, revision_id=revision_id
        )
        status = repository.status(admitted.census_id)
    if unit_is_terminal(status, unit_kind):
        revalidate(request, None)
        state, _accounting = repository.reconcile_terminal(admitted, attempt)
        return PublicationRunOutcome(state, 0, 0)
    revalidate(request, None)
    if not publisher.handler_available(request.census_kind, unit_kind):
        repository.pause(admitted, attempt, reason="child_handler_unavailable")
        return PublicationRunOutcome("paused_with_checkpoint", 0, 0)
    envelope = mapping_envelope(admitted, attempt, request, revision_id)
    _publish_one(
        repository,
        publisher,
        admitted,
        attempt,
        envelope,
        "src.crm_tenant_tasks.run_mapping_child",
        lambda: revalidate(request, None),
    )
    return PublicationRunOutcome("publishing", 1, 0)


def publish_sources(
    repository: StandaloneCrmCensusRepository,
    publisher: ChildPublisher,
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    request: SourceSyncCensusRequest,
    authority: SourceSyncAuthoritySnapshot | None,
    window: FrozenSourceWindow,
    status: StandaloneCrmCensusStatus | None,
    revalidate: Revalidator,
) -> PublicationRunOutcome:
    positive_units = tuple(
        (kind, upper)
        for kind, upper in window.upper_bounds
        if upper > 0 and not unit_is_terminal(status, kind)
    )
    revalidate(request, authority)
    if any(
        not publisher.handler_available(request.census_kind, kind)
        for kind, _upper in positive_units
    ):
        repository.pause(admitted, attempt, reason="child_handler_unavailable")
        return PublicationRunOutcome("paused_with_checkpoint", 0, 0)
    published = 0
    for sequence, (unit_kind, upper_id) in enumerate(window.upper_bounds, start=1):
        if upper_id == 0 or unit_is_terminal(status, unit_kind):
            continue
        revalidate(request, authority)
        _publish_one(
            repository,
            publisher,
            admitted,
            attempt,
            source_envelope(admitted, attempt, request, unit_kind, upper_id, sequence),
            "src.standalone_crm_source_child.run",
            lambda: revalidate(request, authority),
        )
        published += 1
    return PublicationRunOutcome("publishing", published, 0)


def _mapping_identity(
    request: MappingPrepareCensusRequest | MappingRollbackCensusRequest,
) -> tuple[Literal["mapping_prepare", "mapping_rollback"], str]:
    if isinstance(request, MappingPrepareCensusRequest):
        return "mapping_prepare", request.prepared_revision_id
    return "mapping_rollback", request.target_revision_id


def _publication_attempt(
    status: StandaloneCrmCensusStatus, publication: StandaloneCrmPublication
) -> StandaloneCrmAttempt:
    matching = [
        attempt
        for attempt in status.attempts
        if int_field(attempt, "generation") == publication_generation(publication)
    ]
    if len(matching) != 1:
        raise RuntimeError("publication generation is not current")
    return attempt_from_status(matching[0])


def _republish_reserved(
    repository: StandaloneCrmCensusRepository,
    publisher: ChildPublisher,
    admission: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    publication: StandaloneCrmPublication,
    census_kind: str,
    revalidate: Callable[[], None],
) -> None:
    unit_kind = payload_unit_kind(publication.payload_json)
    if not publisher.handler_available(census_kind, unit_kind):
        raise RuntimeError("child handler is unavailable; publication not sent")
    _publish_reserved(repository, publisher, admission, attempt, publication, revalidate)


def _publish_one(
    repository: StandaloneCrmCensusRepository,
    publisher: ChildPublisher,
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    envelope: StandaloneCrmChildEnvelope,
    task_name: str,
    revalidate: Callable[[], None],
) -> None:
    revalidate()
    payload_json = canonical_json(asdict(envelope))
    publication = repository.reserve_publication(
        admission=admitted,
        attempt=attempt,
        unit_kind=envelope.unit_kind,
        sequence=1,
        task_id=envelope.task_id,
        task_name=task_name,
        queue="ingestion",
        payload_json=payload_json,
        payload_digest=envelope.payload_digest,
    )
    _publish_reserved(repository, publisher, admitted, attempt, publication, revalidate)


def _publish_reserved(
    repository: StandaloneCrmCensusRepository,
    publisher: ChildPublisher,
    admitted: StandaloneCrmCensusAdmission,
    attempt: StandaloneCrmAttempt,
    publication: StandaloneCrmPublication,
    revalidate: Callable[[], None],
) -> None:
    try:
        revalidate()
    except Exception:
        repository.mark_publication_ambiguous(admitted, attempt, publication.publication_id)
        raise
    repository.mark_publication_publishing(admitted, attempt, publication.publication_id)
    try:
        revalidate()
        publisher.publish(
            task_name=publication.task_name,
            task_id=publication.task_id,
            queue=publication.queue,
            payload_json=publication.payload_json,
        )
    except Exception:
        repository.mark_publication_ambiguous(admitted, attempt, publication.publication_id)
        raise
    repository.confirm_publication(admitted, attempt, publication.publication_id)
