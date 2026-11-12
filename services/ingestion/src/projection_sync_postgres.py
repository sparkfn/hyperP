"""Postgres writes for the Neo4j -> Postgres read projection (#491).

All statements are parameterized and executed through SQLAlchemy Core; no
value is ever interpolated into SQL text. JSON documents are bound as JSON
text and cast with ``CAST(:payload AS jsonb)``.

Each commit unit is one transaction: the parent's child rows are deleted and
re-inserted, then the parent row is upserted. The projection is the only
writer of these tables; the Rust reader is read-only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Final

from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from src.projection_sync_converters import to_utc_datetime
from src.projection_sync_models import (
    ChildRows,
    IdentityLinkRevisionRow,
    PersonProjectionRow,
    ProjectionState,
    SummaryCounters,
)

_UPSERT_PERSON: Final = text(
    """
    INSERT INTO public.person (
        person_id, canonical_id, status,
        preferred_full_name, preferred_phone, preferred_email, preferred_dob,
        preferred_nric, preferred_address_id, updated_at,
        profile_completeness_score, is_high_value, is_high_risk,
        source_record_count, connection_count, entity_count, identifier_count,
        possible_match_count, system_match_count, order_count, crm_deal_count,
        bankruptcy_case_count, phone_confidence, list_payload, detail_payload
    ) VALUES (
        :person_id, :canonical_id, :status,
        :preferred_full_name, :preferred_phone, :preferred_email, :preferred_dob,
        :preferred_nric, :preferred_address_id, :updated_at,
        :profile_completeness_score, :is_high_value, :is_high_risk,
        :source_record_count, :connection_count, :entity_count, :identifier_count,
        :possible_match_count, :system_match_count, :order_count, :crm_deal_count,
        :bankruptcy_case_count, :phone_confidence,
        CAST(:list_payload AS jsonb), CAST(:detail_payload AS jsonb)
    )
    ON CONFLICT (person_id) DO UPDATE SET
        canonical_id = EXCLUDED.canonical_id,
        status = EXCLUDED.status,
        preferred_full_name = EXCLUDED.preferred_full_name,
        preferred_phone = EXCLUDED.preferred_phone,
        preferred_email = EXCLUDED.preferred_email,
        preferred_dob = EXCLUDED.preferred_dob,
        preferred_nric = EXCLUDED.preferred_nric,
        preferred_address_id = EXCLUDED.preferred_address_id,
        updated_at = EXCLUDED.updated_at,
        profile_completeness_score = EXCLUDED.profile_completeness_score,
        is_high_value = EXCLUDED.is_high_value,
        is_high_risk = EXCLUDED.is_high_risk,
        source_record_count = EXCLUDED.source_record_count,
        connection_count = EXCLUDED.connection_count,
        entity_count = EXCLUDED.entity_count,
        identifier_count = EXCLUDED.identifier_count,
        possible_match_count = EXCLUDED.possible_match_count,
        system_match_count = EXCLUDED.system_match_count,
        order_count = EXCLUDED.order_count,
        crm_deal_count = EXCLUDED.crm_deal_count,
        bankruptcy_case_count = EXCLUDED.bankruptcy_case_count,
        phone_confidence = EXCLUDED.phone_confidence,
        list_payload = EXCLUDED.list_payload,
        detail_payload = EXCLUDED.detail_payload
    """
)

_DELETE_PERSON_IDENTIFIERS: Final = text(
    """
    DELETE FROM public.person_identifier WHERE person_id IN :person_ids
    """
).bindparams(bindparam("person_ids", expanding=True))

_DELETE_PERSON_ADDRESSES: Final = text(
    """
    DELETE FROM public.person_address WHERE person_id IN :person_ids
    """
).bindparams(bindparam("person_ids", expanding=True))

_DELETE_PERSON_CONNECTIONS: Final = text(
    """
    DELETE FROM public.person_connection WHERE person_id IN :person_ids
    """
).bindparams(bindparam("person_ids", expanding=True))

_DELETE_PERSON_SOURCE_RECORDS: Final = text(
    """
    DELETE FROM public.source_record WHERE person_id IN :person_ids
    """
).bindparams(bindparam("person_ids", expanding=True))

_UPSERT_IDENTIFIER: Final = text(
    """
    INSERT INTO public.person_identifier (
        person_id, identifier_type, normalized_value, is_active, is_verified, payload
    ) VALUES (
        :person_id, :identifier_type, :normalized_value, :is_active, :is_verified,
        CAST(:payload AS jsonb)
    )
    ON CONFLICT (person_id, identifier_type, normalized_value) DO UPDATE SET
        is_active = EXCLUDED.is_active,
        is_verified = EXCLUDED.is_verified,
        payload = EXCLUDED.payload
    """
)

_UPSERT_ADDRESS: Final = text(
    """
    INSERT INTO public.person_address (
        person_id, address_id, is_active, unit_number, street_name, city,
        postal_code, country_code, payload
    ) VALUES (
        :person_id, :address_id, :is_active, :unit_number, :street_name, :city,
        :postal_code, :country_code, CAST(:payload AS jsonb)
    )
    ON CONFLICT (person_id, address_id) DO UPDATE SET
        is_active = EXCLUDED.is_active,
        unit_number = EXCLUDED.unit_number,
        street_name = EXCLUDED.street_name,
        city = EXCLUDED.city,
        postal_code = EXCLUDED.postal_code,
        country_code = EXCLUDED.country_code,
        payload = EXCLUDED.payload
    """
)

_UPSERT_CONNECTION: Final = text(
    """
    INSERT INTO public.person_connection (
        person_id, connected_person_id, hops, connection_type, payload
    ) VALUES (
        :person_id, :connected_person_id, :hops, :connection_type,
        CAST(:payload AS jsonb)
    )
    ON CONFLICT (person_id, connected_person_id) DO UPDATE SET
        hops = EXCLUDED.hops,
        connection_type = EXCLUDED.connection_type,
        payload = EXCLUDED.payload
    """
)

_UPSERT_SOURCE_RECORD: Final = text(
    """
    INSERT INTO public.source_record (
        source_record_pk, person_id, entity_key, source_key, record_type,
        lifecycle_status, is_active, observed_at, payload
    ) VALUES (
        :source_record_pk, :person_id, :entity_key, :source_key, :record_type,
        :lifecycle_status, :is_active, :observed_at, CAST(:payload AS jsonb)
    )
    ON CONFLICT (source_record_pk, person_id) DO UPDATE SET
        entity_key = EXCLUDED.entity_key,
        source_key = EXCLUDED.source_key,
        record_type = EXCLUDED.record_type,
        lifecycle_status = EXCLUDED.lifecycle_status,
        is_active = EXCLUDED.is_active,
        observed_at = EXCLUDED.observed_at,
        payload = EXCLUDED.payload
    """
)

_INSERT_IDENTITY_LINK_REVISION: Final = text(
    """
    INSERT INTO public.identity_link_rev (
        event_id, global_revision, link_key, revision
    ) VALUES (
        :event_id, :global_revision, :link_key, CAST(:revision AS jsonb)
    )
    ON CONFLICT (event_id) DO UPDATE SET
        global_revision = EXCLUDED.global_revision,
        link_key = EXCLUDED.link_key,
        revision = EXCLUDED.revision
    """
)

_READ_STATE: Final = text(
    """
    SELECT high_water, last_success, current_revision, identity_stream_ready
    FROM public.summary_counter
    WHERE id = 'global'
    """
)

_UPSERT_STATE: Final = text(
    """
    INSERT INTO public.summary_counter (
        id, high_water, last_success, current_revision, identity_stream_ready, summary
    ) VALUES (
        'global', :high_water, :last_success, :current_revision, :identity_stream_ready,
        CAST(:summary AS jsonb)
    )
    ON CONFLICT (id) DO UPDATE SET
        high_water = EXCLUDED.high_water,
        last_success = EXCLUDED.last_success,
        current_revision = EXCLUDED.current_revision,
        identity_stream_ready = EXCLUDED.identity_stream_ready,
        summary = EXCLUDED.summary
    """
)

_DELETE_PERSONS: Final = text(
    """
    DELETE FROM public.person WHERE person_id IN :person_ids
    """
).bindparams(bindparam("person_ids", expanding=True))

_COUNT_PERSONS: Final = text("SELECT count(*) AS person_count FROM public.person")

_LIST_PERSON_IDS: Final = text(
    """
    SELECT person_id FROM public.person
    WHERE person_id > :after_person_id
    ORDER BY person_id
    LIMIT :page_size
    """
)


def _json_text(payload: Mapping[str, object]) -> str:
    """Serialize a payload document for a ``jsonb`` cast."""
    return json.dumps(dict(payload))


def _person_params(row: PersonProjectionRow) -> dict[str, object]:
    """Build the bound parameters of one person upsert."""
    return {
        "person_id": row.person_id,
        "canonical_id": row.canonical_id,
        "status": row.status,
        "preferred_full_name": row.preferred_full_name,
        "preferred_phone": row.preferred_phone,
        "preferred_email": row.preferred_email,
        "preferred_dob": row.preferred_dob,
        "preferred_nric": row.preferred_nric,
        "preferred_address_id": row.preferred_address_id,
        "updated_at": row.updated_at,
        "profile_completeness_score": row.profile_completeness_score,
        "is_high_value": row.is_high_value,
        "is_high_risk": row.is_high_risk,
        "source_record_count": row.source_record_count,
        "connection_count": row.connection_count,
        "entity_count": row.entity_count,
        "identifier_count": row.identifier_count,
        "possible_match_count": row.possible_match_count,
        "system_match_count": row.system_match_count,
        "order_count": row.order_count,
        "crm_deal_count": row.crm_deal_count,
        "bankruptcy_case_count": row.bankruptcy_case_count,
        "phone_confidence": row.phone_confidence,
        "list_payload": _json_text(row.list_payload),
        "detail_payload": _json_text(row.detail_payload),
    }


class ProjectionPostgresWriter:
    """Transactional writer for the Postgres read projection."""

    def __init__(self, dsn: str) -> None:
        """Create the engine for the given DSN without logging the DSN."""
        self._engine: Engine = create_engine(
            dsn,
            poolclass=NullPool,
            future=True,
        )

    def close(self) -> None:
        """Dispose of the engine and its pooled connections."""
        self._engine.dispose()

    def read_state(self) -> ProjectionState | None:
        """Read the durable projection state, or ``None`` when uninitialized."""
        with self._engine.begin() as conn:
            row = conn.execute(_READ_STATE).mappings().first()
        if row is None:
            return None
        return ProjectionState(
            high_water=to_utc_datetime(row["high_water"]),
            last_success=to_utc_datetime(row["last_success"]),
            current_revision=int(row["current_revision"]),
            identity_stream_ready=bool(row["identity_stream_ready"]),
        )

    def write_persons(
        self,
        persons: Sequence[PersonProjectionRow],
        children: ChildRows,
    ) -> None:
        """Replace the projected rows of the given persons in one transaction."""
        person_ids = [row.person_id for row in persons]
        if not person_ids:
            return
        with self._engine.begin() as conn:
            _delete_person_children(conn, person_ids)
            conn.execute(_UPSERT_PERSON, [_person_params(row) for row in persons])
            if children.identifiers:
                conn.execute(
                    _UPSERT_IDENTIFIER,
                    [
                        {
                            "person_id": row.person_id,
                            "identifier_type": row.identifier_type,
                            "normalized_value": row.normalized_value,
                            "is_active": row.is_active,
                            "is_verified": row.is_verified,
                            "payload": _json_text(row.payload),
                        }
                        for row in children.identifiers
                    ],
                )
            if children.addresses:
                conn.execute(
                    _UPSERT_ADDRESS,
                    [
                        {
                            "person_id": row.person_id,
                            "address_id": row.address_id,
                            "is_active": row.is_active,
                            "unit_number": row.unit_number,
                            "street_name": row.street_name,
                            "city": row.city,
                            "postal_code": row.postal_code,
                            "country_code": row.country_code,
                            "payload": _json_text(row.payload),
                        }
                        for row in children.addresses
                    ],
                )
            if children.connections:
                conn.execute(
                    _UPSERT_CONNECTION,
                    [
                        {
                            "person_id": row.person_id,
                            "connected_person_id": row.connected_person_id,
                            "hops": row.hops,
                            "connection_type": row.connection_type,
                            "payload": _json_text(row.payload),
                        }
                        for row in children.connections
                    ],
                )
            if children.source_records:
                conn.execute(
                    _UPSERT_SOURCE_RECORD,
                    [
                        {
                            "source_record_pk": row.source_record_pk,
                            "person_id": row.person_id,
                            "entity_key": row.entity_key,
                            "source_key": row.source_key,
                            "record_type": row.record_type,
                            "lifecycle_status": row.lifecycle_status,
                            "is_active": row.is_active,
                            "observed_at": row.observed_at,
                            "payload": _json_text(row.payload),
                        }
                        for row in children.source_records
                    ],
                )

    def append_revisions(self, revisions: Sequence[IdentityLinkRevisionRow]) -> None:
        """Append immutable identity-link revisions in one transaction."""
        if not revisions:
            return
        with self._engine.begin() as conn:
            conn.execute(
                _INSERT_IDENTITY_LINK_REVISION,
                [
                    {
                        "event_id": row.event_id,
                        "global_revision": row.global_revision,
                        "link_key": row.link_key,
                        "revision": _json_text(row.revision),
                    }
                    for row in revisions
                ],
            )

    def write_state(self, state: ProjectionState, counters: SummaryCounters) -> None:
        """Persist the durable projection state and the summary counters."""
        with self._engine.begin() as conn:
            conn.execute(
                _UPSERT_STATE,
                {
                    "high_water": state.high_water,
                    "last_success": state.last_success,
                    "current_revision": state.current_revision,
                    "identity_stream_ready": state.identity_stream_ready,
                    "summary": _json_text(counters.as_json()),
                },
            )

    def delete_persons(self, person_ids: Sequence[str]) -> None:
        """Delete projected persons that no longer exist in the graph."""
        if not person_ids:
            return
        with self._engine.begin() as conn:
            conn.execute(_DELETE_PERSONS, {"person_ids": list(person_ids)})

    def count_persons(self) -> int:
        """Return the number of projected persons."""
        with self._engine.begin() as conn:
            return int(conn.execute(_COUNT_PERSONS).scalar_one())

    def list_person_ids(self, after_person_id: str, page_size: int) -> list[str]:
        """Return a keyset page of projected person ids."""
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    _LIST_PERSON_IDS,
                    {"after_person_id": after_person_id, "page_size": page_size},
                )
                .mappings()
                .all()
            )
        return [str(row["person_id"]) for row in rows]


def _delete_person_children(conn: Connection, person_ids: Sequence[str]) -> None:
    """Remove every child collection row of the given persons."""
    ids = list(person_ids)
    conn.execute(_DELETE_PERSON_IDENTIFIERS, {"person_ids": ids})
    conn.execute(_DELETE_PERSON_ADDRESSES, {"person_ids": ids})
    conn.execute(_DELETE_PERSON_CONNECTIONS, {"person_ids": ids})
    conn.execute(_DELETE_PERSON_SOURCE_RECORDS, {"person_ids": ids})
