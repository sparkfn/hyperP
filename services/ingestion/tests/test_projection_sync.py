"""Unit contracts for the Neo4j -> Postgres read projection (#491).

Every test here uses fakes only: no Neo4j connection and no Postgres
connection is opened.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError
from src.celery_app import celery_app
from src.config import Settings
from src.projection_sync import (
    MARK_OVERLAP_SECONDS,
    projection_sync_task,
    run_projection_sync,
)
from src.projection_sync_graph import ProjectionGraph
from src.projection_sync_models import (
    ChildRows,
    IdentityLinkRevisionRow,
    PersonProjectionRow,
    ProjectionState,
    SummaryCounters,
)
from src.projection_sync_postgres import ProjectionPostgresWriter

_TASK_NAME: Final[str] = "src.projection_sync.projection_sync_task"
_INGESTION_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
_MARK: Final[datetime] = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _person_map(person_id: str) -> dict[str, object]:
    """Return the graph person map of one projected person."""
    return {
        "person_id": person_id,
        "status": "active",
        "is_high_value": False,
        "is_high_risk": False,
        "preferred_full_name": "Test Person",
        "preferred_phone": "+6599999999",
        "preferred_email": "test@example.com",
        "preferred_dob": "1990-01-01",
        "preferred_nric": None,
        "profile_completeness_score": 0.5,
        "golden_profile_computed_at": None,
        "golden_profile_version": "1",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def _list_row(person_id: str) -> dict[str, object]:
    """Return the graph list-view row of one projected person."""
    return {
        "person_id": person_id,
        "canonical_id": None,
        "person": _person_map(person_id),
        "preferred_address": None,
        "preferred_address_id": None,
        "source_record_count": 1,
        "connection_count": 0,
        "phone_confidence": None,
        "entities": [],
        "entity_count": 0,
        "identifier_count": 1,
        "possible_match_count": 0,
        "system_match_count": 0,
        "order_count": 0,
        "crm_deal_count": 0,
        "bankruptcy_case_count": 0,
    }


def _detail_row(person_id: str) -> dict[str, object]:
    """Return the graph detail-view row of one projected person."""
    person = _person_map(person_id)
    person["preferred_race_ethnicity"] = None
    return {
        "requested_person_id": person_id,
        "canonical_id": None,
        "person": person,
        "preferred_address": None,
        "source_record_count": 1,
        "connection_count": 0,
        "lifetime_value": None,
    }


def _revision_row(global_revision: int) -> dict[str, object]:
    """Return the graph row of one identity-link revision."""
    return {
        "event_id": f"event-{global_revision}",
        "global_revision": global_revision,
        "link_key": "link-1",
        "source_system": "crm",
        "source_instance_id": "instance-1",
        "source_entity_type": "deal",
        "source_entity_id": "deal-1",
        "identity_policy_version": "v1",
        "link_status": "resolved",
        "hyperp_person_id": "p1",
        "resolution_kind": "deterministic",
        "resolution_revision": 1,
        "effective_at": datetime(2026, 1, 1, tzinfo=UTC),
        "match_decision_id": None,
        "review_case_id": None,
        "supersedes_event_id": None,
    }


class _FakeGraph(ProjectionGraph):
    """Serves canned projection rows and records the calls a run makes."""

    def __init__(self) -> None:
        """Initialise the canned rows and the call log."""
        self.all_ids: list[str] = []
        self.changed_ids: list[str] = []
        self.neighbours: dict[str, list[str]] = {}
        self.existing_ids: set[str] = set()
        self.existing_lookups = 0
        self.marks: list[str] = []
        self.revisions: list[dict[str, object]] = []
        self.failing_person_id: str | None = None

    def all_person_ids(self, after_person_id: str, page_size: int) -> list[str]:
        """Return a keyset page of every canned person id."""
        return [pid for pid in self.all_ids if pid > after_person_id][:page_size]

    def changed_person_ids(self, mark: str, after_person_id: str, page_size: int) -> list[str]:
        """Return a keyset page of the canned changed person ids."""
        self.marks.append(mark)
        return [pid for pid in self.changed_ids if pid > after_person_id][:page_size]

    def neighbour_person_ids(self, person_ids: list[str]) -> list[str]:
        """Return the canned one-hop neighbours of the given persons."""
        found: list[str] = []
        for person_id in person_ids:
            for neighbour in self.neighbours.get(person_id, []):
                if neighbour not in found:
                    found.append(neighbour)
        return found

    def person_list_rows(self, person_ids: list[str]) -> list[dict[str, object]]:
        """Return the canned list-view rows, failing for the marked person."""
        rows: list[dict[str, object]] = []
        for person_id in person_ids:
            if person_id == self.failing_person_id:
                raise RuntimeError("graph read failed")
            rows.append(_list_row(person_id))
        return rows

    def person_detail_rows(self, person_ids: list[str]) -> list[dict[str, object]]:
        """Return the canned detail-view rows."""
        return [_detail_row(person_id) for person_id in person_ids]

    def person_identifier_rows(self, person_ids: list[str]) -> list[dict[str, object]]:
        """Return no identifier rows."""
        return []

    def person_address_rows(self, person_ids: list[str]) -> list[dict[str, object]]:
        """Return no address rows."""
        return []

    def person_connection_rows(self, person_ids: list[str]) -> list[dict[str, object]]:
        """Return no connection rows."""
        return []

    def person_source_record_rows(self, person_ids: list[str]) -> list[dict[str, object]]:
        """Return no source-record rows."""
        return []

    def person_count(self) -> int:
        """Return the canned total number of persons in the graph."""
        return len(self.existing_ids)

    def existing_person_ids(self, person_ids: list[str]) -> list[str]:
        """Return the canned subset of person ids that still exists."""
        self.existing_lookups += 1
        return [pid for pid in person_ids if pid in self.existing_ids]

    def summary_counters(self, bound: str) -> dict[str, object]:
        """Return the canned summary counters."""
        return {
            "all_profiles_count": 2,
            "high_risk_count": 0,
            "high_value_count": 0,
            "no_contact_count": 0,
            "deals_this_month_count": 1,
            "all_deals_count": 3,
        }

    def identity_link_state(self) -> dict[str, object]:
        """Return the canned identity-link readiness probe."""
        return {
            "current_revision": 1,
            "baseline_completed_at": datetime(2026, 1, 1, tzinfo=UTC),
            "migration_completed_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

    def identity_link_revisions(
        self, after_revision: int, page_size: int
    ) -> list[dict[str, object]]:
        """Return the canned identity-link revisions after the given cursor."""
        rows = [row for row in self.revisions if int(row["global_revision"]) > after_revision]
        return rows[:page_size]


class _FakeWriter(ProjectionPostgresWriter):
    """Records the writes a projection run performs."""

    def __init__(self) -> None:
        """Initialise the recorded state and write log."""
        self.state: ProjectionState | None = None
        self.states_written: list[ProjectionState] = []
        self.persons_written: list[str] = []
        self.revision_batches: list[list[IdentityLinkRevisionRow]] = []
        self.deleted: list[str] = []
        self.person_ids: list[str] = []
        self.counters: list[SummaryCounters] = []

    def read_state(self) -> ProjectionState | None:
        """Return the recorded durable state."""
        return self.state

    def write_persons(self, persons: list[PersonProjectionRow], children: ChildRows) -> None:
        """Record the persons written by one page."""
        for person in persons:
            self.persons_written.append(person.person_id)
            if person.person_id not in self.person_ids:
                self.person_ids.append(person.person_id)

    def append_revisions(self, revisions: list[IdentityLinkRevisionRow]) -> None:
        """Record the appended identity-link revisions."""
        self.revision_batches.append(list(revisions))

    def write_state(self, state: ProjectionState, counters: SummaryCounters) -> None:
        """Record the durable state and counters."""
        self.state = state
        self.states_written.append(state)
        self.counters.append(counters)

    def delete_persons(self, person_ids: list[str]) -> None:
        """Record the deleted persons."""
        self.deleted.extend(person_ids)
        for person_id in person_ids:
            if person_id in self.person_ids:
                self.person_ids.remove(person_id)

    def count_persons(self) -> int:
        """Return the number of recorded person ids."""
        return len(self.person_ids)

    def list_person_ids(self, after_person_id: str, page_size: int) -> list[str]:
        """Return a keyset page of the recorded person ids."""
        return [pid for pid in self.person_ids if pid > after_person_id][:page_size]

    def close(self) -> None:
        """Close the fake writer without touching a database."""


class _FakeClient:
    """A Neo4j client stand-in that owns no connection."""

    def close(self) -> None:
        """Close the fake client."""


def _settings(rust_spike_enabled: bool) -> Settings:
    """Return settings with the spike switch set as requested."""
    return Settings(
        neo4j_password="test-password",
        rust_spike_enabled=rust_spike_enabled,
        projection_postgres_dsn="postgresql://postgres:secret@localhost:5432/postgres",
    )


def test_the_task_is_a_noop_while_the_rust_spike_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.projection_sync.get_settings", lambda: _settings(False))

    assert projection_sync_task.run() == {"skipped": True}


def test_the_task_runs_when_the_rust_spike_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.projection_sync.get_settings", lambda: _settings(True))
    graph = _FakeGraph()
    writer = _FakeWriter()

    monkeypatch.setattr("src.projection_sync.ProjectionGraph", lambda client: graph)
    monkeypatch.setattr("src.projection_sync.ProjectionPostgresWriter", lambda dsn: writer)
    monkeypatch.setattr("src.projection_sync.Neo4jClient", lambda settings: _FakeClient())

    result = projection_sync_task.run()

    assert result["skipped"] is False
    assert result["persons_written"] == 0


def test_the_first_run_backfills_every_person() -> None:
    graph = _FakeGraph()
    graph.all_ids = ["p1", "p2", "p3"]
    writer = _FakeWriter()

    result = run_projection_sync(graph, writer)

    assert sorted(writer.persons_written) == ["p1", "p2", "p3"]
    assert result.persons_scanned == 3
    assert graph.marks == []


def test_a_later_run_selects_the_persons_changed_since_the_stored_mark() -> None:
    graph = _FakeGraph()
    graph.changed_ids = ["p2"]
    writer = _FakeWriter()
    writer.state = ProjectionState(
        high_water=_MARK,
        last_success=_MARK,
        current_revision=0,
        identity_stream_ready=False,
    )

    run_projection_sync(graph, writer)

    assert writer.persons_written == ["p2"]
    expected_mark = (_MARK - timedelta(seconds=MARK_OVERLAP_SECONDS)).isoformat()
    assert graph.marks[0].startswith(expected_mark[:19])


def test_a_relationship_only_change_projects_the_affected_neighbour() -> None:
    graph = _FakeGraph()
    graph.changed_ids = ["p1"]
    graph.neighbours = {"p1": ["p2"]}
    writer = _FakeWriter()
    writer.state = ProjectionState(
        high_water=_MARK,
        last_success=_MARK,
        current_revision=0,
        identity_stream_ready=False,
    )

    run_projection_sync(graph, writer)

    assert sorted(writer.persons_written) == ["p1", "p2"]


def test_a_second_run_without_changes_writes_no_person() -> None:
    graph = _FakeGraph()
    writer = _FakeWriter()
    writer.state = ProjectionState(
        high_water=_MARK,
        last_success=_MARK,
        current_revision=0,
        identity_stream_ready=False,
    )

    result = run_projection_sync(graph, writer)

    assert result.persons_written == 0
    assert writer.persons_written == []


def test_a_repeated_run_rewrites_the_same_persons() -> None:
    graph = _FakeGraph()
    graph.all_ids = ["p1", "p2"]
    graph.changed_ids = ["p1", "p2"]
    writer = _FakeWriter()

    run_projection_sync(graph, writer)
    first_run = list(writer.persons_written)
    writer.persons_written.clear()
    run_projection_sync(graph, writer)

    assert first_run == ["p1", "p2"]
    assert writer.persons_written == ["p1", "p2"]


def test_the_watermark_does_not_advance_when_a_page_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.projection_sync.PERSON_PAGE_SIZE", 2)
    graph = _FakeGraph()
    graph.all_ids = ["p1", "p2", "p3"]
    graph.failing_person_id = "p3"
    writer = _FakeWriter()

    with pytest.raises(RuntimeError):
        run_projection_sync(graph, writer)

    assert writer.states_written == []
    assert writer.state is None


def test_the_state_advances_only_after_every_page_committed() -> None:
    graph = _FakeGraph()
    graph.all_ids = ["p1"]
    writer = _FakeWriter()

    result = run_projection_sync(graph, writer)

    assert len(writer.states_written) == 1
    state = writer.states_written[0]
    assert state.high_water == result.high_water
    assert state.last_success is not None
    assert state.identity_stream_ready is True


def test_persons_deleted_from_the_graph_are_reconciled() -> None:
    graph = _FakeGraph()
    graph.existing_ids = {"p1"}
    writer = _FakeWriter()
    writer.person_ids = ["p1", "p2"]

    result = run_projection_sync(graph, writer)

    assert writer.deleted == ["p2"]
    assert result.persons_deleted == 1


def test_equal_totals_skip_the_removal_sweep() -> None:
    graph = _FakeGraph()
    graph.existing_ids = {"p1", "p2"}
    writer = _FakeWriter()
    writer.person_ids = ["p1", "p2"]

    result = run_projection_sync(graph, writer)

    assert result.persons_deleted == 0
    assert graph.existing_lookups == 0


def test_a_failed_run_propagates_so_the_next_tick_is_the_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.projection_sync.get_settings", lambda: _settings(True))
    graph = _FakeGraph()
    graph.all_ids = ["p1"]
    graph.failing_person_id = "p1"
    monkeypatch.setattr("src.projection_sync.ProjectionGraph", lambda client: graph)
    monkeypatch.setattr("src.projection_sync.ProjectionPostgresWriter", lambda dsn: _FakeWriter())
    monkeypatch.setattr("src.projection_sync.Neo4jClient", lambda settings: _FakeClient())

    with pytest.raises(RuntimeError, match="graph read failed"):
        projection_sync_task.run()


def test_identity_revisions_are_appended_in_order() -> None:
    graph = _FakeGraph()
    graph.revisions = [_revision_row(1), _revision_row(2)]
    writer = _FakeWriter()

    run_projection_sync(graph, writer)

    appended = [row for batch in writer.revision_batches for row in batch]
    assert [row.global_revision for row in appended] == [1, 2]
    assert writer.states_written[0].current_revision == 2


def test_identity_revisions_stop_at_a_gap() -> None:
    graph = _FakeGraph()
    graph.revisions = [_revision_row(1), _revision_row(3)]
    writer = _FakeWriter()

    run_projection_sync(graph, writer)

    assert writer.revision_batches == []
    assert writer.states_written[0].current_revision == 0


def test_the_projection_task_is_routed_to_the_lifecycle_queue() -> None:
    assert celery_app.conf.task_routes[_TASK_NAME] == {"queue": "lifecycle"}
    assert _TASK_NAME in celery_app.tasks
    assert "src.projection_sync" in celery_app.conf.include


def _projection_beat_entry(environment: dict[str, str]) -> object:
    """Import the Celery app in a clean interpreter and return its projection beat entry."""
    script = (
        "import json; from src.celery_app import _beat_schedule; "
        "print(json.dumps(_beat_schedule.get('projection-sync')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_INGESTION_ROOT,
        env={**os.environ, "NEO4J_PASSWORD": "test-password", **environment},
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_beat_schedule_runs_the_projection_only_while_the_spike_is_enabled() -> None:
    # A clean interpreter keeps the module-level Celery app of this session untouched.
    assert _projection_beat_entry({"RUST_SPIKE_ENABLED": "false"}) is None
    assert _projection_beat_entry(
        {
            "RUST_SPIKE_ENABLED": "true",
            "PROJECTION_SYNC_INTERVAL_SECONDS": "45",
            "PROJECTION_POSTGRES_DSN": "postgresql://postgres:secret@localhost:5432/postgres",
        }
    ) == {
        "task": _TASK_NAME,
        "schedule": 45.0,
        "args": [],
        "options": {"queue": "lifecycle", "expires": 45.0},
    }


def test_the_projection_dsn_is_required_only_while_the_spike_is_enabled() -> None:
    disabled = Settings(neo4j_password="test-password")
    assert disabled.projection_postgres_dsn.get_secret_value() == ""
    assert disabled.rust_spike_enabled is False

    with pytest.raises(ValidationError):
        Settings(neo4j_password="test-password", rust_spike_enabled=True)

    enabled = Settings(
        neo4j_password="test-password",
        rust_spike_enabled=True,
        projection_postgres_dsn="postgresql://postgres:secret@localhost:5432/postgres",
    )
    assert enabled.projection_sync_interval_seconds == 30
