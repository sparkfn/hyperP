"""Bounded orchestration for the Neo4j -> Postgres read projection (#491).

One run rebuilds the Postgres projection from the graph:

* the run captures an upper-bound timestamp and reads bounded keyset pages;
* the first run backfills every person, later runs select the persons changed
  since the stored high-water plus the persons affected by those changes;
* each page is written in one Postgres transaction (child rows replaced, parent
  row upserted);
* the high-water mark, the identity-link revision cursor and the last-success
  stamp advance only after every page of the run has committed;
* a failure raises, so the mark does not advance and the run is redelivered.

The projection never writes to Neo4j.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Final

from src.celery_app import celery_app
from src.config import get_settings
from src.graph.client import Neo4jClient
from src.projection_sync_converters import to_int, to_utc_datetime
from src.projection_sync_graph import ProjectionGraph
from src.projection_sync_mapper import (
    build_child_rows,
    build_detail_payload,
    build_list_payload,
    build_person_row,
    build_revision_row,
)
from src.projection_sync_models import (
    ChildRows,
    IdentityLinkRevisionRow,
    PersonProjectionRow,
    ProjectionRunResult,
    ProjectionState,
    SummaryCounters,
)
from src.projection_sync_postgres import ProjectionPostgresWriter

logger = logging.getLogger(__name__)

#: Persons projected per keyset page and per Postgres transaction.
PERSON_PAGE_SIZE: Final[int] = 200
#: Identity-link revisions read per page.
REVISION_PAGE_SIZE: Final[int] = 500
#: Person ids checked per removal-reconciliation page.
REMOVAL_PAGE_SIZE: Final[int] = 500
#: Grace window re-scanned around the stored high-water mark, so a change
#: committed just before the mark is not lost.
MARK_OVERLAP_SECONDS: Final[int] = 300


def _iso(value: datetime) -> str:
    """Render a datetime as a UTC ISO 8601 string."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _chunk(values: list[str], size: int) -> list[list[str]]:
    """Split a list into fixed-size chunks."""
    return [values[index : index + size] for index in range(0, len(values), size)]


def _project_person_page(
    graph: ProjectionGraph,
    person_ids: list[str],
) -> tuple[list[PersonProjectionRow], ChildRows]:
    """Project one page of persons into the Postgres projection."""
    list_rows = graph.person_list_rows(person_ids)
    detail_rows = {
        str(row["requested_person_id"]): row for row in graph.person_detail_rows(person_ids)
    }
    persons: list[PersonProjectionRow] = []
    for list_row in list_rows:
        person_id = str(list_row["person_id"])
        detail_row = detail_rows.get(person_id)
        if detail_row is None:
            # The person vanished between the two reads; removal reconciliation owns it.
            continue
        list_payload = build_list_payload(list_row)
        detail_payload = build_detail_payload(detail_row)
        persons.append(build_person_row(list_row, list_payload, detail_payload))
    children = build_child_rows(
        graph.person_identifier_rows(person_ids),
        graph.person_address_rows(person_ids),
        graph.person_connection_rows(person_ids),
        graph.person_source_record_rows(person_ids),
    )
    return persons, children


def _write_person_page(
    graph: ProjectionGraph,
    writer: ProjectionPostgresWriter,
    person_ids: list[str],
) -> tuple[int, int, int, int, int]:
    """Project and write one page of persons; return the written counts."""
    persons, children = _project_person_page(graph, person_ids)
    if not persons:
        return 0, 0, 0, 0, 0
    writer.write_persons(persons, children)
    return (
        len(persons),
        len(children.identifiers),
        len(children.addresses),
        len(children.connections),
        len(children.source_records),
    )


def _affected_person_ids(
    graph: ProjectionGraph,
    state: ProjectionState | None,
    page_size: int,
) -> tuple[list[str], int]:
    """Return the person ids to project in this run and the number scanned."""
    scanned = 0
    affected: list[str] = []
    seen: set[str] = set()
    after = ""
    mark: str | None = None
    if state is not None and state.high_water is not None:
        mark = _iso(state.high_water - timedelta(seconds=MARK_OVERLAP_SECONDS))
    while True:
        if mark is None:
            page = graph.all_person_ids(after, page_size)
        else:
            page = graph.changed_person_ids(mark, after, page_size)
        if not page:
            break
        scanned += len(page)
        after = page[-1]
        candidates = list(page)
        if mark is not None:
            # A backfill already visits every person; only a delta needs its neighbours.
            candidates.extend(graph.neighbour_person_ids(page))
        for person_id in candidates:
            if person_id not in seen:
                seen.add(person_id)
                affected.append(person_id)
    return affected, scanned


def _append_identity_revisions(
    graph: ProjectionGraph,
    writer: ProjectionPostgresWriter,
    current_revision: int,
) -> tuple[int, int]:
    """Append the identity-link revisions after the stored cursor.

    Revisions are appended in order and the cursor only advances over a
    contiguous run: a gap in the graph stream stops the append so the Rust
    reader surfaces the gap instead of silently skipping a revision.
    """
    appended = 0
    cursor = current_revision
    while True:
        rows = graph.identity_link_revisions(cursor, REVISION_PAGE_SIZE)
        if not rows:
            break
        batch: list[IdentityLinkRevisionRow] = []
        expected = cursor + 1
        for row in rows:
            revision = build_revision_row(row)
            if revision.global_revision != expected:
                logger.error(
                    "identity-link revision gap detected: expected=%d found=%d",
                    expected,
                    revision.global_revision,
                )
                return appended, cursor
            batch.append(revision)
            expected += 1
        writer.append_revisions(batch)
        appended += len(batch)
        cursor = expected - 1
    return appended, cursor


def _reconcile_removals(
    graph: ProjectionGraph,
    writer: ProjectionPostgresWriter,
) -> int:
    """Delete projected persons that no longer exist in the graph.

    Every graph person is projected before this runs, so equal totals mean the
    projection holds no stale row and the full id sweep can be skipped.
    """
    if writer.count_persons() == graph.person_count():
        return 0
    deleted = 0
    after = ""
    while True:
        person_ids = writer.list_person_ids(after, REMOVAL_PAGE_SIZE)
        if not person_ids:
            break
        after = person_ids[-1]
        existing = set(graph.existing_person_ids(person_ids))
        missing = [person_id for person_id in person_ids if person_id not in existing]
        if missing:
            writer.delete_persons(missing)
            deleted += len(missing)
    return deleted


def run_projection_sync(
    graph: ProjectionGraph,
    writer: ProjectionPostgresWriter,
) -> ProjectionRunResult:
    """Run one bounded projection pass over the graph."""
    started = time.monotonic()
    state = writer.read_state()
    bound = datetime.now(UTC)
    affected, scanned = _affected_person_ids(graph, state, PERSON_PAGE_SIZE)
    persons_written = 0
    identifiers_written = 0
    addresses_written = 0
    connections_written = 0
    source_records_written = 0
    for page in _chunk(affected, PERSON_PAGE_SIZE):
        counts = _write_person_page(graph, writer, page)
        persons_written += counts[0]
        identifiers_written += counts[1]
        addresses_written += counts[2]
        connections_written += counts[3]
        source_records_written += counts[4]
    counters = _summary_counters(graph, bound)
    current_revision = state.current_revision if state is not None else 0
    revisions_appended, current_revision = _append_identity_revisions(
        graph, writer, current_revision
    )
    persons_deleted = _reconcile_removals(graph, writer)
    identity_stream_ready = _identity_stream_ready(graph)
    writer.write_state(
        ProjectionState(
            high_water=bound,
            last_success=datetime.now(UTC),
            current_revision=current_revision,
            identity_stream_ready=identity_stream_ready,
        ),
        counters,
    )
    duration = time.monotonic() - started
    logger.info(
        "projection sync completed persons_scanned=%d persons_written=%d "
        "identifiers=%d addresses=%d connections=%d source_records=%d "
        "revisions=%d deleted=%d high_water=%s duration_seconds=%.3f",
        scanned,
        persons_written,
        identifiers_written,
        addresses_written,
        connections_written,
        source_records_written,
        revisions_appended,
        persons_deleted,
        _iso(bound),
        duration,
    )
    return ProjectionRunResult(
        persons_scanned=scanned,
        persons_written=persons_written,
        identifiers_written=identifiers_written,
        addresses_written=addresses_written,
        connections_written=connections_written,
        source_records_written=source_records_written,
        revisions_appended=revisions_appended,
        persons_deleted=persons_deleted,
        high_water=bound,
        duration_seconds=duration,
    )


def _summary_counters(graph: ProjectionGraph, bound: datetime) -> SummaryCounters:
    """Read the six person-list summary counters."""
    row = graph.summary_counters(_iso(bound))
    return SummaryCounters(
        all_profiles_count=to_int(row.get("all_profiles_count")),
        high_risk_count=to_int(row.get("high_risk_count")),
        high_value_count=to_int(row.get("high_value_count")),
        no_contact_count=to_int(row.get("no_contact_count")),
        deals_this_month_count=to_int(row.get("deals_this_month_count")),
        all_deals_count=to_int(row.get("all_deals_count")),
    )


def _identity_stream_ready(graph: ProjectionGraph) -> bool:
    """Report whether the identity-link stream is ready to be read."""
    row = graph.identity_link_state()
    baseline = to_utc_datetime(row.get("baseline_completed_at"))
    migration = to_utc_datetime(row.get("migration_completed_at"))
    return baseline is not None and migration is not None


@celery_app.task(  # type: ignore[untyped-decorator]  # Celery ships no type stubs
    name="src.projection_sync.projection_sync_task",
)
def projection_sync_task() -> dict[str, object]:
    """Project the graph into the Postgres read projection.

    A failure propagates without advancing the mark; the next beat tick is the retry, so
    no task retries itself and failed runs cannot pile up.
    """
    settings = get_settings()
    if not settings.rust_spike_enabled:
        logger.info("projection sync skipped: rust spike disabled")
        return {"skipped": True}
    client = Neo4jClient(settings)
    writer = ProjectionPostgresWriter(settings.projection_postgres_dsn.get_secret_value())
    try:
        result = run_projection_sync(ProjectionGraph(client), writer)
    except Exception as error:
        logger.error(
            "projection sync failed; the projection mark was not advanced: %s",
            type(error).__name__,
        )
        raise
    finally:
        writer.close()
        client.close()
    return {
        "skipped": False,
        "persons_scanned": result.persons_scanned,
        "persons_written": result.persons_written,
        "revisions_appended": result.revisions_appended,
        "persons_deleted": result.persons_deleted,
    }
