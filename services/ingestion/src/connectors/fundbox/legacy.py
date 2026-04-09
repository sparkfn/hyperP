"""Connector for legacy/migrated profiles (``source_key=fundbox:legacy``)."""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import select
from sqlalchemy.engine import Connection

from src.connectors.fundbox.base import FundboxConnectorBase
from src.connectors.fundbox.builders import (
    IdentifierBag,
    build_envelope,
    format_address,
    serialize_row,
    to_iso,
)
from src.connectors.fundbox.schema import (
    log_legacy_profile_addresses,
    log_legacy_profiles,
)
from src.models import JsonValue


class FundboxLegacyConnector(FundboxConnectorBase):
    """Yields one source record per row in ``log_legacy_profiles``.

    Mirrors :class:`FundboxConnector` but reads from the legacy snapshot
    tables that predate the current ``users`` table.
    """

    def get_source_key(self) -> str:
        return "fundbox:legacy"

    def build_records(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        primary_stmt = select(log_legacy_profiles).order_by(log_legacy_profiles.c.id)
        for chunk in self._chunked(
            self._stream(conn, primary_stmt), self._resolved_chunk_size()
        ):
            user_ids = [row.user_id for row in chunk if row.user_id is not None]
            addresses_by_user = self._fetch_grouped(
                conn, log_legacy_profile_addresses, "user_id", user_ids
            )
            for row in chunk:
                user_addresses = addresses_by_user.get(row.user_id, [])
                ids = IdentifierBag()
                ids.add("nric", row.nric, verified=True)
                ids.add("email", row.email)
                ids.add("phone", row.mobile_number)
                ids.add("phone", row.whatsapp_phone)
                ids.add("social:facebook", row.facebook_id)

                yield build_envelope(
                    source_record_id=f"fundbox-legacy-{row.id}",
                    observed_at=to_iso(row.updated_at or row.created_at),
                    identifiers=ids.items,
                    attributes={
                        "full_name": row.full_name,
                        "dob": to_iso(row.date_of_birth),
                        "gender": row.gender,
                        "nationality": row.nationality,
                        "address": format_address(user_addresses[0])
                        if user_addresses
                        else None,
                    },
                    raw_payload={
                        "legacy_profile": serialize_row(row),
                        "addresses": [serialize_row(a) for a in user_addresses],
                    },
                )
