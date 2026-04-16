"""Connector for current Fundbox users (``source_key=fundbox_consumer_backend``)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

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
    addresses,
    basic_plus_profiles,
    basic_profiles,
    device_ids,
    last_logins,
    social_accounts,
    users,
)
from src.models import JsonValue


class FundboxConnector(FundboxConnectorBase):
    """Yields one source record per current Fundbox user.

    Streams ``users`` joined with ``basic_profiles`` and ``basic_plus_profiles``
    in chunks; for each chunk, sidecar tables (addresses, social accounts,
    device IDs, last logins) are batch-loaded by ``user_id IN (...)`` so the
    total memory footprint stays bounded regardless of source size.
    """

    def get_source_key(self) -> str:
        return "fundbox_consumer_backend"

    def build_records(self, conn: Connection) -> Iterator[dict[str, JsonValue]]:
        primary_stmt = (
            select(
                users.c.id.label("user_id"),
                users.c.email.label("user_email"),
                users.c.mobile_number.label("user_mobile"),
                users.c.created_at.label("user_created_at"),
                users.c.updated_at.label("user_updated_at"),
                basic_profiles.c.nric,
                basic_profiles.c.full_name,
                basic_profiles.c.date_of_birth,
                basic_profiles.c.gender,
                basic_profiles.c.nationality,
                basic_profiles.c.email.label("profile_email"),
                basic_profiles.c.mobile_number.label("profile_mobile"),
                basic_plus_profiles.c.whatsapp_phone,
                basic_plus_profiles.c.facebook_id,
            )
            .select_from(
                users.outerjoin(basic_profiles, basic_profiles.c.user_id == users.c.id)
                .outerjoin(
                    basic_plus_profiles,
                    basic_plus_profiles.c.user_id == users.c.id,
                )
            )
            .order_by(users.c.id)
        )

        for chunk in self._chunked(self._stream(conn, primary_stmt), self._resolved_chunk_size()):
            user_ids = [row.user_id for row in chunk]
            addresses_by_user = self._fetch_grouped(conn, addresses, "user_id", user_ids)
            socials_by_user = self._fetch_grouped(conn, social_accounts, "user_id", user_ids)
            devices_by_user = self._fetch_grouped(conn, device_ids, "user_id", user_ids)
            last_login_by_user = self._fetch_scalar_map(
                conn, last_logins, "user_id", "last_logged_in", user_ids
            )

            for row in chunk:
                yield self._build_one(
                    row,
                    addresses_by_user.get(row.user_id, []),
                    socials_by_user.get(row.user_id, []),
                    devices_by_user.get(row.user_id, []),
                    to_iso(last_login_by_user.get(row.user_id)),
                )

    @staticmethod
    def _collect_identifiers(
        row: Any,
        user_socials: list[Any],
        user_devices: list[Any],
        last_login: str | None,
    ) -> IdentifierBag:
        ids = IdentifierBag()
        ids.add("nric", row.nric, verified=True)
        ids.add("email", row.user_email, last_confirmed_at=last_login)
        ids.add("email", row.profile_email, last_confirmed_at=last_login)
        ids.add("phone", row.user_mobile, last_confirmed_at=last_login)
        ids.add("phone", row.profile_mobile, last_confirmed_at=last_login)
        ids.add("phone", row.whatsapp_phone, last_confirmed_at=last_login)
        ids.add("social:facebook", row.facebook_id)
        for social in user_socials:
            provider = (social._mapping.get("provider") or "").strip().lower()
            if provider:
                ids.add(f"social:{provider}", social._mapping.get("provider_id"))
        for device in user_devices:
            ids.add("device_id", device._mapping.get("device_id"))
        return ids

    @staticmethod
    def _build_one(
        row: Any,
        user_addresses: list[Any],
        user_socials: list[Any],
        user_devices: list[Any],
        last_login: str | None,
    ) -> dict[str, JsonValue]:
        ids = FundboxConnector._collect_identifiers(
            row, user_socials, user_devices, last_login,
        )
        primary_address = format_address(user_addresses[0]) if user_addresses else None
        return build_envelope(
            source_record_id=f"fundbox_consumer_backend-user-{row.user_id}",
            observed_at=to_iso(row.user_updated_at or row.user_created_at),
            identifiers=ids.items,
            attributes={
                "full_name": row.full_name,
                "dob": to_iso(row.date_of_birth),
                "gender": row.gender,
                "nationality": row.nationality,
                "address": primary_address,
            },
            raw_payload={
                "user": {
                    "user_id": row.user_id,
                    "user_email": row.user_email,
                    "user_mobile": row.user_mobile,
                    "user_created_at": to_iso(row.user_created_at),
                    "user_updated_at": to_iso(row.user_updated_at),
                    "nric": row.nric,
                    "full_name": row.full_name,
                    "date_of_birth": to_iso(row.date_of_birth),
                    "gender": row.gender,
                    "nationality": row.nationality,
                    "profile_email": row.profile_email,
                    "profile_mobile": row.profile_mobile,
                    "whatsapp_phone": row.whatsapp_phone,
                    "facebook_id": row.facebook_id,
                },
                "addresses": [serialize_row(a) for a in user_addresses],
                "social_accounts": [serialize_row(s) for s in user_socials],
                "device_ids": [d._mapping.get("device_id") for d in user_devices],
                "last_logged_in": last_login,
            },
        )
