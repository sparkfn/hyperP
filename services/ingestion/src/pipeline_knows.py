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

import json
import logging

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.client import Neo4jClient

logger = logging.getLogger(__name__)


def _parse_contact_payload(raw_payload_str: object) -> dict[str, object] | None:
    """Parse raw_payload JSON; return None on failure."""
    try:
        if isinstance(raw_payload_str, str):
            parsed: object = json.loads(raw_payload_str)
            return parsed if isinstance(parsed, dict) else None
        if isinstance(raw_payload_str, dict):
            return raw_payload_str
        return None
    except (TypeError, ValueError):
        return None


def _resolve_both_persons(
    tx: ManagedTransaction, declarer_sr_id: object, contact_pk: str,
) -> tuple[str, str] | None:
    """Resolve declarer and contact person IDs. Returns None if either is missing or same."""
    declarer = tx.run(
        queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_ID, source_record_id=declarer_sr_id,
    ).single()
    contact = tx.run(
        queries.RESOLVE_PERSON_FROM_SOURCE_RECORD_PK, source_record_pk=contact_pk,
    ).single()
    if declarer is None or contact is None:
        return None
    d_id: str = declarer["person_id"]
    c_id: str = contact["person_id"]
    return (d_id, c_id) if d_id != c_id else None


def _link_one_contact(
    tx: ManagedTransaction, contact_source_record_pk: str, raw_payload: dict[str, object],
) -> bool:
    """Resolve both sides of a contact record and MERGE the KNOWS edge."""
    declarer_sr_id = raw_payload.get("linked_to_source_record_id")
    if not declarer_sr_id:
        return False
    pair = _resolve_both_persons(tx, declarer_sr_id, contact_source_record_pk)
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
        source_system_key="fundbox_consumer_backend",
        source_record_pk=contact_source_record_pk,
        relationship_label=relationship_label,
        relationship_category=_category_for(relationship_label),
        status=cp_dict.get("status") or "declared",
        approved_at=cp_dict.get("approved_at"),
    )
    return True


def materialize_knows_from_contacts(
    client: Neo4jClient, *, batch_size: int = 500
) -> int:
    """Walk every contact SourceRecord and link declarer → contact via KNOWS.

    Paginates through contact records using a source_record_pk cursor so
    arbitrarily large contact sets are fully processed. Returns the number
    of KNOWS edges created.
    """
    total_linked = 0
    cursor = ""

    while True:
        def _work(
            tx: ManagedTransaction, cursor_param: str = cursor
        ) -> tuple[int, str | None]:
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
                last_pk = pk
                raw = _parse_contact_payload(row["raw_payload"])
                if raw is None:
                    continue
                if _link_one_contact(tx, pk, raw):
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
    if any(t in lower for t in ("spouse", "parent", "child", "sibling", "family")):
        return "family"
    return "social"
