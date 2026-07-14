"""Materialise declared ``KNOWS`` edges after identity resolution.

The Fundbox ``contacts`` connector emits one SourceRecord per emergency
contact / next-of-kin row. The identity pipeline resolves each such
record to its own Person, then this module runs at end-of-ingest to
walk the contact records and materialise a
``(declarer_person)-[:KNOWS]->(contact_person)`` edge.

Why a separate pass: both ends of the KNOWS edge must already exist as
Persons. Running it *after* the main ingestion run lets the pipeline
build both sides independently — and lets a partial run re-attempt
resolution on its next tick without surgery on the identity path.
"""

from __future__ import annotations

import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient
from src.models import JsonValue, SourceRecordEnvelope
from src.raw_payload import decode_raw_payload

logger = logging.getLogger(__name__)


def _declarer_source_system_key(relationship_source_system_key: str) -> str:
    """Return the identity source owning a relationship record's declarer."""
    suffix = ":contacts"
    if relationship_source_system_key.endswith(suffix):
        return relationship_source_system_key[: -len(suffix)]
    return relationship_source_system_key


def _relationship_blueprint(envelope: SourceRecordEnvelope) -> dict[str, JsonValue] | None:
    raw = envelope.raw_payload
    declarer = raw.get("linked_to_source_record_id") or raw.get("primary_source_record_id")
    if not isinstance(declarer, str) or not declarer:
        return None
    contact = raw.get("contact")
    contact_row = contact if isinstance(contact, dict) else {}
    label_raw = (
        raw.get("link_type")
        or raw.get("relationship_label")
        or raw.get("relationship_to_primary")
        or contact_row.get("relationship")
    )
    label = str(label_raw) if label_raw is not None else None
    is_contact = raw.get("linked_to_source_record_id") is not None
    if not is_contact and (label is None or not label.strip()):
        return None
    status_raw = contact_row.get("status")
    status = status_raw if isinstance(status_raw, str) and status_raw else None
    approved_at_raw = contact_row.get("approved_at")
    approved_at = approved_at_raw if isinstance(approved_at_raw, str) else None
    return {
        "declarer_source_record_id": declarer,
        "relationship_label": label,
        "relationship_category": _category_for(label),
        "status": status or ("declared" if is_contact else "pending"),
        "approved_at": approved_at,
        "source_system_key": envelope.source_system,
        "declarer_source_system_key": _declarer_source_system_key(envelope.source_system),
    }


def knows_projection_blueprints(envelope: SourceRecordEnvelope) -> list[JsonValue]:
    blueprint = _relationship_blueprint(envelope)
    return [] if blueprint is None else [blueprint]


def activate_knows_projection(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    person_id: str,
    source_record_pk: str,
) -> bool:
    """Activate this record's KNOWS assertion, or report unresolved declared endpoints."""
    blueprint = _relationship_blueprint(envelope)
    if blueprint is None:
        return True
    declarer = tx.run(
        queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID,
        source_record_id=blueprint["declarer_source_record_id"],
        source_system_key=blueprint["declarer_source_system_key"],
    ).single()
    if declarer is None:
        return False
    declarer_person_id = str(declarer["person_id"])
    if declarer_person_id == person_id:
        return True
    tx.run(
        queries.LINK_PERSON_KNOWS,
        declarer_person_id=declarer_person_id,
        contact_person_id=person_id,
        source_system_key=blueprint["source_system_key"],
        source_record_pk=source_record_pk,
        relationship_label=blueprint["relationship_label"],
        relationship_category=blueprint["relationship_category"],
        status=blueprint["status"],
        approved_at=blueprint["approved_at"],
    )
    return True


def _resolve_both_persons(
    tx: ManagedTransaction,
    declarer_sr_id: object,
    declarer_source_system_key: str,
    contact_pk: str,
) -> tuple[str, str] | None:
    """Resolve declarer and contact person IDs. Returns None if either is missing or same."""
    declarer = tx.run(
        queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID,
        source_record_id=declarer_sr_id,
        source_system_key=declarer_source_system_key,
    ).single()
    contact = tx.run(
        queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_PK,
        source_record_pk=contact_pk,
    ).single()
    if declarer is None or contact is None:
        return None
    d_id: str = declarer["person_id"]
    c_id: str = contact["person_id"]
    return (d_id, c_id) if d_id != c_id else None


def _link_one_contact(
    tx: ManagedTransaction,
    contact_source_record_pk: str,
    source_system_key: str,
    raw_payload: dict[str, JsonValue],
) -> bool:
    """Resolve both sides of a contact record and MERGE the KNOWS edge."""
    declarer_sr_id = raw_payload.get("linked_to_source_record_id")
    if not declarer_sr_id:
        return False
    pair = _resolve_both_persons(
        tx,
        declarer_sr_id,
        _declarer_source_system_key(source_system_key),
        contact_source_record_pk,
    )
    if pair is None:
        return False
    declarer_person_id, contact_person_id = pair

    cp = raw_payload.get("contact") or {}
    cp_dict = cp if isinstance(cp, dict) else {}
    raw_label = raw_payload.get("link_type") or cp_dict.get("relationship")
    relationship_label = str(raw_label) if raw_label is not None else None

    tx.run(
        queries.LINK_PERSON_KNOWS,
        declarer_person_id=declarer_person_id,
        contact_person_id=contact_person_id,
        source_system_key=source_system_key,
        source_record_pk=contact_source_record_pk,
        relationship_label=relationship_label,
        relationship_category=_category_for(relationship_label),
        status=cp_dict.get("status") or "declared",
        approved_at=cp_dict.get("approved_at"),
    )
    return True


def _link_one_chat_relationship(
    tx: ManagedTransaction,
    contact_source_record_pk: str,
    source_system_key: str,
    raw_payload: dict[str, JsonValue],
) -> bool:
    declarer_sr_id = raw_payload.get("primary_source_record_id")
    if not declarer_sr_id:
        return False
    relationship_label_raw = raw_payload.get("relationship_label") or raw_payload.get(
        "relationship_to_primary"
    )
    if not relationship_label_raw:
        return False
    pair = _resolve_both_persons(
        tx,
        declarer_sr_id,
        _declarer_source_system_key(source_system_key),
        contact_source_record_pk,
    )
    if pair is None:
        return False
    declarer_person_id, contact_person_id = pair
    relationship_label = str(relationship_label_raw)
    tx.run(
        queries.LINK_PERSON_KNOWS,
        declarer_person_id=declarer_person_id,
        contact_person_id=contact_person_id,
        source_system_key=source_system_key,
        source_record_pk=contact_source_record_pk,
        relationship_label=relationship_label,
        relationship_category=_category_for(relationship_label),
        status="pending",
        approved_at=None,
    )
    return True


def materialize_knows_from_chat_relationships(
    client: Neo4jClient,
    *,
    batch_size: int = 500,
) -> int:
    total_linked = 0
    cursor = ""

    while True:

        def _work(tx: ManagedTransaction, cursor_param: str = cursor) -> tuple[int, str | None]:
            result = tx.run(
                queries.SCAN_CHAT_RELATIONSHIP_SOURCE_RECORDS,
                cursor=cursor_param,
                batch_size=batch_size,
            )
            rows = list(result)
            if not rows:
                return 0, None
            newly_linked = 0
            last_pk: str = cursor_param
            for row in rows:
                pk: str = row["source_record_pk"]
                source_system_key: str = row["source_system_key"]
                last_pk = pk
                raw = decode_raw_payload(row["raw_payload"])
                if raw is None:
                    logger.warning("Skipping source record %s: raw_payload undecodable", pk)
                    continue
                if _link_one_chat_relationship(tx, pk, source_system_key, raw):
                    newly_linked += 1
            return newly_linked, last_pk

        with client.session() as session:
            newly_linked, last_pk = session.execute_write(_work)
        if last_pk is None:
            break
        total_linked += newly_linked
        cursor = last_pk

    if total_linked:
        logger.info("Materialized %d KNOWS edges from chat relationships", total_linked)
    else:
        logger.debug("No new chat relationship KNOWS edges materialized")
    return total_linked


def materialize_knows_from_contacts(client: Neo4jClient, *, batch_size: int = 500) -> int:
    """Walk every contact SourceRecord and link declarer → contact via KNOWS.

    Paginates through contact records using a source_record_pk cursor so
    arbitrarily large contact sets are fully processed. Returns the number
    of KNOWS edges created.
    """
    total_linked = 0
    cursor = ""

    while True:

        def _work(tx: ManagedTransaction, cursor_param: str = cursor) -> tuple[int, str | None]:
            result = tx.run(
                queries.SCAN_CONTACT_SOURCE_RECORDS,
                cursor=cursor_param,
                batch_size=batch_size,
            )
            rows = list(result)
            if not rows:
                return 0, None
            newly_linked = 0
            last_pk: str = cursor_param
            for row in rows:
                pk: str = row["source_record_pk"]
                source_system_key: str = row["source_system_key"]
                last_pk = pk
                raw = decode_raw_payload(row["raw_payload"])
                if raw is None:
                    logger.warning("Skipping source record %s: raw_payload undecodable", pk)
                    continue
                if _link_one_contact(tx, pk, source_system_key, raw):
                    newly_linked += 1
            return newly_linked, last_pk

        with client.session() as session:
            newly_linked, last_pk = session.execute_write(_work)
        if last_pk is None:
            break
        total_linked += newly_linked
        cursor = last_pk

    if total_linked:
        logger.info("Materialized %d KNOWS edges from contact records", total_linked)
    else:
        logger.debug("No new KNOWS edges materialized")
    return total_linked


def _category_for(label: str | None) -> str:
    """Coarse-grained category derived from the raw relationship label."""
    if not label:
        return "social"
    lower = label.strip().lower()
    if any(t in lower for t in ("emergency", "next of kin", "nok")):
        return "emergency_contact"
    if any(t in lower for t in ("referrer", "referral")):
        return "referrer"
    if any(
        t in lower
        for t in (
            "spouse",
            "parent",
            "child",
            "sibling",
            "brother",
            "sister",
            "family",
        )
    ):
        return "family"
    return "social"
