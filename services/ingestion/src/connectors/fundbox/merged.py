"""Connector for pre-existing merge lineage (``source_key=fundbox:merged``)."""

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
from src.connectors.fundbox.schema import merged_users
from src.models import JsonValue


class FundboxMergedUsersConnector(FundboxConnectorBase):
    """Yields one source record per row in ``merged_users``.

    Each record represents the *old* identity that the source system has
    already merged into a surviving user. The ``merge_hint`` payload field
    points at the surviving user's source record id so the pipeline can
    replay the merge as a ``MERGED_INTO`` edge instead of re-matching.
    """

    def get_source_key(self) -> str:
        return "fundbox:merged"

    def build_records(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        stmt = select(merged_users).order_by(merged_users.c.id)
        for row in self._stream(conn, stmt):
            ids = IdentifierBag()
            ids.add("nric", row.nric, verified=True)
            ids.add("email", row.email)
            ids.add("phone", row.mobile_number)

            yield build_envelope(
                source_record_id=f"fundbox-merged-{row.id}",
                observed_at=to_iso(row.updated_at or row.created_at),
                identifiers=ids.items,
                attributes={},
                raw_payload={
                    "merged_user": serialize_row(row),
                    "merge_hint": {
                        "merged_into_source_record_id": (
                            f"fundbox-user-{row.new_user_id}" if row.new_user_id else None
                        ),
                        "surviving_identifiers": {
                            "nric": row.new_nric,
                            "email": row.new_email,
                            "phone": row.new_mobile_number,
                        },
                    },
                },
            )
