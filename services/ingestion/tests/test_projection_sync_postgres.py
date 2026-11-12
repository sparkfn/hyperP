"""Postgres contracts for the read projection writer (#491).

The module is skipped unless ``PROJECTION_TEST_POSTGRES_DSN`` points at a
disposable Postgres. When the variable is set, a connection failure fails the
tests loudly instead of skipping them.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.pool import NullPool
from src.projection_sync_models import (
    ChildRows,
    IdentityLinkRevisionPayload,
    IdentityLinkRevisionRow,
    ListedPersonPayload,
    PersonAddressRow,
    PersonConnectionRow,
    PersonIdentifierRow,
    PersonPayload,
    PersonProjectionRow,
    ProjectionState,
    SourceRecordRow,
    SummaryCounters,
)
from src.projection_sync_postgres import ProjectionPostgresWriter

_DSN: Final[str] = os.environ.get("PROJECTION_TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(not _DSN, reason="PROJECTION_TEST_POSTGRES_DSN is not set")

_SCHEMA_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "services" / "rust-read" / "postgres" / "01-schema.sql"
)
_PERSON_ID: Final[str] = "test-projection-person"
_PERSON_PREFIX: Final[str] = "test-projection"


def _list_payload(person_id: str) -> ListedPersonPayload:
    """Return the API list-view document of one projected person."""
    return ListedPersonPayload(
        person_id=person_id,
        status="active",
        is_high_value=False,
        is_high_risk=False,
        preferred_full_name="Test Person",
        preferred_phone="+6599999999",
        preferred_email="test@example.com",
        preferred_dob="1990-01-01",
        preferred_address=None,
        preferred_nric=None,
        preferred_race_ethnicity=None,
        profile_completeness_score=0.5,
        golden_profile_computed_at=None,
        golden_profile_version="1",
        source_record_count=1,
        connection_count=0,
        lifetime_value=None,
        loyalty=None,
        vehicles=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        phone_confidence=None,
        entities=[],
        entity_count=0,
        identifier_count=1,
        possible_match_count=0,
        system_match_count=0,
        order_count=0,
        crm_deal_count=0,
        bankruptcy_case_count=0,
        preferred_dob_display="01 Jan 1990",
        preferred_dob_invalid=False,
    )


def _detail_payload(person_id: str) -> PersonPayload:
    """Return the API detail-view document of one projected person."""
    return PersonPayload(
        person_id=person_id,
        status="active",
        is_high_value=False,
        is_high_risk=False,
        preferred_full_name="Test Person",
        preferred_phone="+6599999999",
        preferred_email="test@example.com",
        preferred_dob="1990-01-01",
        preferred_address=None,
        preferred_nric=None,
        preferred_race_ethnicity=None,
        profile_completeness_score=0.5,
        golden_profile_computed_at=None,
        golden_profile_version="1",
        source_record_count=1,
        connection_count=0,
        lifetime_value=None,
        loyalty=None,
        vehicles=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def _person_row(person_id: str) -> PersonProjectionRow:
    """Return the projected parent row of one person."""
    return PersonProjectionRow(
        person_id=person_id,
        canonical_id=None,
        status="active",
        preferred_full_name="Test Person",
        preferred_phone="+6599999999",
        preferred_email="test@example.com",
        preferred_dob="1990-01-01",
        preferred_nric=None,
        preferred_address_id=None,
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        profile_completeness_score=0.5,
        is_high_value=False,
        is_high_risk=False,
        source_record_count=1,
        connection_count=0,
        entity_count=0,
        identifier_count=1,
        possible_match_count=0,
        system_match_count=0,
        order_count=0,
        crm_deal_count=0,
        bankruptcy_case_count=0,
        phone_confidence=None,
        list_payload=_list_payload(person_id),
        detail_payload=_detail_payload(person_id),
    )


def _children(person_id: str) -> ChildRows:
    """Return the child collections of one projected person."""
    return ChildRows(
        identifiers=(
            PersonIdentifierRow(
                person_id=person_id,
                identifier_type="phone",
                normalized_value="6599999999",
                is_active=True,
                is_verified=False,
                payload={"last_confirmed_at": None, "source_system_key": "eko_phppos"},
            ),
        ),
        addresses=(
            PersonAddressRow(
                person_id=person_id,
                address_id="addr-1",
                is_active=True,
                unit_number="01",
                street_name="Main Street",
                city="Singapore",
                postal_code="123456",
                country_code="SG",
                payload={"street_number": "1", "normalized_full": "1 Main Street"},
            ),
        ),
        connections=(
            PersonConnectionRow(
                person_id=person_id,
                connected_person_id="test-projection-other",
                hops=1,
                connection_type="identifier",
                payload={},
            ),
        ),
        source_records=(
            SourceRecordRow(
                source_record_pk="sr-1",
                person_id=person_id,
                entity_key="entity-1",
                source_key="eko_phppos",
                record_type="identity",
                lifecycle_status="active",
                is_active=True,
                observed_at=datetime(2026, 1, 1, tzinfo=UTC),
                payload={
                    "source_record_id": "1",
                    "source_record_version": "1",
                    "link_status": "linked",
                    "ingested_at": None,
                },
            ),
        ),
    )


def _revision_payload(event_id: str, global_revision: int) -> IdentityLinkRevisionPayload:
    """Return the public revision document of one identity-link revision."""
    return IdentityLinkRevisionPayload(
        event_id=event_id,
        global_revision=global_revision,
        source_system="crm",
        source_instance_id="instance-1",
        source_entity_type="deal",
        source_entity_id="deal-1",
        identity_policy_version="v1",
        link_status="resolved",
        hyperp_person_id=_PERSON_ID,
        resolution_kind="deterministic",
        resolution_revision=1,
        effective_at="2026-01-01T00:00:00Z",
        match_decision_id=None,
        review_case_id=None,
        supersedes_event_id=None,
    )


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """Return an engine on the disposable database with the schema applied."""
    eng = create_engine(_DSN, poolclass=NullPool, future=True)
    _await_database(eng)
    with eng.connect() as conn:
        conn.exec_driver_sql(_SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    try:
        yield eng
    finally:
        with eng.begin() as conn:
            conn.execute(
                text("DELETE FROM public.person WHERE person_id LIKE :prefix"),
                {"prefix": f"{_PERSON_PREFIX}%"},
            )
            conn.execute(
                text("DELETE FROM public.identity_link_rev WHERE event_id LIKE :prefix"),
                {"prefix": f"{_PERSON_PREFIX}%"},
            )
        eng.dispose()


def _await_database(eng: Engine, attempts: int = 30, delay_seconds: float = 2.0) -> None:
    """Wait for the disposable database to accept connections.

    A database that never becomes reachable fails the tests loudly; it is
    never skipped.
    """
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except OperationalError as error:
            last_error = error
            time.sleep(delay_seconds)
    raise AssertionError(f"the disposable projection database is unreachable: {last_error}")


@pytest.fixture
def writer(engine: Engine) -> Iterator[ProjectionPostgresWriter]:
    """Return a projection writer on the disposable database."""
    instance = ProjectionPostgresWriter(_DSN)
    try:
        yield instance
    finally:
        instance.close()
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM public.person WHERE person_id LIKE :prefix"),
                {"prefix": f"{_PERSON_PREFIX}%"},
            )
            conn.execute(
                text("DELETE FROM public.identity_link_rev WHERE event_id LIKE :prefix"),
                {"prefix": f"{_PERSON_PREFIX}%"},
            )
            conn.execute(
                text(
                    "UPDATE public.summary_counter SET high_water = NULL, last_success = NULL, "
                    "current_revision = 0, identity_stream_ready = false, summary = NULL "
                    "WHERE id = 'global'"
                )
            )


def _rows(engine: Engine, statement: str, **params: object) -> list[tuple[object, ...]]:
    """Return the rows of one parameterized statement."""
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(statement), params)]


def test_repeated_writes_produce_identical_rows(
    writer: ProjectionPostgresWriter, engine: Engine
) -> None:
    person = _person_row(_PERSON_ID)
    children = _children(_PERSON_ID)

    writer.write_persons([person], children)
    first = _snapshot(engine, _PERSON_ID)
    writer.write_persons([person], children)
    second = _snapshot(engine, _PERSON_ID)

    assert first == second
    assert len(_rows(engine, "SELECT person_id FROM public.person")) == 1
    assert len(_rows(engine, "SELECT * FROM public.person_identifier")) == 1
    assert len(_rows(engine, "SELECT * FROM public.person_address")) == 1
    assert len(_rows(engine, "SELECT * FROM public.person_connection")) == 1
    assert len(_rows(engine, "SELECT * FROM public.source_record")) == 1


def test_a_removed_child_disappears(writer: ProjectionPostgresWriter, engine: Engine) -> None:
    person = _person_row(_PERSON_ID)
    children = _children(_PERSON_ID)
    writer.write_persons([person], children)

    without_identifier = ChildRows(
        identifiers=(),
        addresses=children.addresses,
        connections=children.connections,
        source_records=children.source_records,
    )
    writer.write_persons([person], without_identifier)

    assert _rows(engine, "SELECT * FROM public.person_identifier") == []
    assert len(_rows(engine, "SELECT * FROM public.person_address")) == 1


def test_deleting_a_person_removes_its_children(
    writer: ProjectionPostgresWriter, engine: Engine
) -> None:
    writer.write_persons([_person_row(_PERSON_ID)], _children(_PERSON_ID))

    writer.delete_persons([_PERSON_ID])

    assert _rows(engine, "SELECT * FROM public.person") == []
    assert _rows(engine, "SELECT * FROM public.person_identifier") == []
    assert _rows(engine, "SELECT * FROM public.source_record") == []


def test_an_identity_link_revision_rejects_a_non_allow_listed_key(
    writer: ProjectionPostgresWriter, engine: Engine
) -> None:
    payload = dict(_revision_payload(f"{_PERSON_PREFIX}-event-1", 1))
    payload["raw_evidence"] = "not-allow-listed"

    with pytest.raises(IntegrityError):
        writer.append_revisions(
            [
                IdentityLinkRevisionRow(
                    event_id=f"{_PERSON_PREFIX}-event-1",
                    global_revision=1,
                    link_key="link-1",
                    revision=payload,
                )
            ]
        )

    assert _rows(engine, "SELECT * FROM public.identity_link_rev") == []


def test_an_allow_listed_identity_link_revision_is_stored(
    writer: ProjectionPostgresWriter, engine: Engine
) -> None:
    writer.append_revisions(
        [
            IdentityLinkRevisionRow(
                event_id=f"{_PERSON_PREFIX}-event-2",
                global_revision=2,
                link_key="link-1",
                revision=_revision_payload(f"{_PERSON_PREFIX}-event-2", 2),
            )
        ]
    )

    stored = _rows(
        engine,
        "SELECT event_id, global_revision, link_key, revision FROM public.identity_link_rev",
    )
    assert len(stored) == 1
    assert stored[0][0] == f"{_PERSON_PREFIX}-event-2"


def test_the_summary_counter_stays_a_singleton(
    writer: ProjectionPostgresWriter, engine: Engine
) -> None:
    state = writer.read_state()
    assert state is not None
    assert state.current_revision == 0

    writer.write_state(
        ProjectionState(
            high_water=datetime(2026, 1, 2, tzinfo=UTC),
            last_success=datetime(2026, 1, 2, tzinfo=UTC),
            current_revision=7,
            identity_stream_ready=True,
        ),
        SummaryCounters(
            all_profiles_count=1,
            high_risk_count=0,
            high_value_count=0,
            no_contact_count=0,
            deals_this_month_count=0,
            all_deals_count=0,
        ),
    )

    rows = _rows(
        engine,
        "SELECT id, current_revision, identity_stream_ready FROM public.summary_counter",
    )
    assert rows == [("global", 7, True)]

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO public.summary_counter (id, current_revision) VALUES ('other', 1)"
                )
            )


def test_an_older_run_cannot_move_the_projection_state_backwards(
    writer: ProjectionPostgresWriter,
) -> None:
    counters = SummaryCounters(
        all_profiles_count=1,
        high_risk_count=0,
        high_value_count=0,
        no_contact_count=0,
        deals_this_month_count=0,
        all_deals_count=0,
    )
    newer = datetime(2026, 1, 3, tzinfo=UTC)
    older = datetime(2026, 1, 2, tzinfo=UTC)
    writer.write_state(
        ProjectionState(
            high_water=newer, last_success=newer, current_revision=9, identity_stream_ready=True
        ),
        counters,
    )

    writer.write_state(
        ProjectionState(
            high_water=older, last_success=older, current_revision=4, identity_stream_ready=True
        ),
        counters,
    )

    state = writer.read_state()
    assert state is not None
    assert state.high_water == newer
    assert state.last_success == newer
    assert state.current_revision == 9


def _snapshot(engine: Engine, person_id: str) -> tuple[object, ...]:
    """Return the projected rows of one person as a comparable tuple."""
    person = _rows(
        engine,
        "SELECT person_id, status, preferred_full_name, preferred_phone, "
        "preferred_email, preferred_dob, list_payload, detail_payload "
        "FROM public.person WHERE person_id = :person_id",
        person_id=person_id,
    )
    identifiers = _rows(
        engine,
        "SELECT identifier_type, normalized_value, is_active, is_verified, payload "
        "FROM public.person_identifier WHERE person_id = :person_id ORDER BY identifier_type",
        person_id=person_id,
    )
    addresses = _rows(
        engine,
        "SELECT address_id, is_active, street_name, city, payload "
        "FROM public.person_address WHERE person_id = :person_id ORDER BY address_id",
        person_id=person_id,
    )
    connections = _rows(
        engine,
        "SELECT connected_person_id, hops, connection_type "
        "FROM public.person_connection WHERE person_id = :person_id ORDER BY connected_person_id",
        person_id=person_id,
    )
    source_records = _rows(
        engine,
        "SELECT source_record_pk, entity_key, source_key, record_type, lifecycle_status, "
        "is_active FROM public.source_record WHERE person_id = :person_id "
        "ORDER BY source_record_pk",
        person_id=person_id,
    )
    return (person, identifiers, addresses, connections, source_records)
