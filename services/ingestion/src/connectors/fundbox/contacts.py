"""Connector for emergency-contact people (``source_key=fundbox:contacts``)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.connectors.fundbox.base import FundboxConnectorBase
from src.connectors.fundbox.builders import (
    IdentifierBag,
    build_envelope,
    serialize_row,
    to_iso,
)
from src.connectors.fundbox.schema import contacts
from src.models import JsonValue


class FundboxContactsConnector(FundboxConnectorBase):
    """Yields one source record per ``contacts`` row.

    Each emergency contact becomes its own source record so the matcher can
    resolve them against existing Persons. The ``raw_payload`` carries
    ``linked_to_source_record_id`` so a downstream pass can materialize the
    Person-to-Person relationship (``LINKED_TO`` / ``FAMILY_OF`` / etc.).
    """

    def get_source_key(self) -> str:
        return "fundbox:contacts"

    def build_records(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        stmt = select(contacts).order_by(contacts.c.id)
        for row in self._stream(conn, stmt):
            ids = IdentifierBag()
            ids.add("phone", row.mobile_number)
            yield build_envelope(
                source_record_id=f"fundbox-contact-{row.id}",
                observed_at=to_iso(row.updated_at or row.created_at),
                identifiers=ids.items,
                attributes={
                    "full_name": row.full_name,
                    "relationship_to_referrer": row.relationship,
                },
                raw_payload={
                    "contact": serialize_row(row),
                    "linked_to_source_record_id": f"fundbox-user-{row.user_id}",
                    "link_type": row.relationship,
                },
            )
