"""OneDiver dump connectors (``source_key=onediver`` and ``onediver:sales``).

OneDiver is a scuba / water-sports business-management platform (MySQL). The
``onediver`` source yields ``identity`` records (one per non-deleted ``profiles``
row) plus ``relationship`` records for the up-to-two emergency contacts in
``profile_emergencies``. The ``onediver:sales`` source yields ``sales`` records
from ``sales_orders``, linked to a profile by ``billing_contact_email`` — the
only reliable sales-to-person link in this dump (96% coverage; the integer FKs
reference a shop-side address book absent from the dump).

Dump-only: the dump reader infers table columns from the dump's own
``CREATE TABLE`` statements, so the table sets map table names to ``None``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

from src.connectors.base import SourceConnector
from src.connectors.dumps.reader import DumpRow, load_dump_tables
from src.connectors.fundbox.builders import (
    IdentifierBag,
    address_from_row,
    build_envelope,
    coerce_float,
    serialize_row,
    to_iso,
    to_iso_first,
)
from src.models import JsonValue

# Mirrors ``TableSpec`` in ``dumps/connectors.py``; duplicated here to avoid an
# import cycle (``dumps/connectors.py`` imports the connector classes below).
TableSpec = Mapping[str, Sequence[str] | None]

ONEDIVER_TABLES: TableSpec = {
    "profiles": None,
    "profile_emergencies": None,
    "users": None,
    "accounts": None,
}

ONEDIVER_SALES_TABLES: TableSpec = {
    "sales_orders": None,
    "profiles": None,
}

_KIN_SLOTS = (
    ("kin1", "contact_first_name", "contact_last_name", "contact_number", "relation"),
    ("kin2", "kin2_fname", "kin2_lname", "kin2_contact", "kin2_relation"),
)


def _str(row: DumpRow, col: str) -> str | None:
    value = row.get(col)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(row: DumpRow, col: str) -> int:
    value = row.get(col)
    if isinstance(value, int):
        return value
    if value is None:
        return 0
    try:
        return int(str(value).strip())
    except ValueError:
        return 0


def _full_name(row: DumpRow) -> str | None:
    first = _str(row, "first_name")
    last = _str(row, "last_name")
    parts = [p for p in (first, last) if p]
    return " ".join(parts) or None


def _address_dict(row: DumpRow) -> dict[str, JsonValue]:
    """Map onediver address columns onto the keys ``address_from_row`` reads."""
    return {
        "address_1": row.get("address"),
        "address_2": row.get("address2"),
        "city": row.get("city"),
        "state": row.get("state"),
        "country": row.get("lk_country_code"),
        "postal_code": row.get("zip_code"),
    }


def _build_identity_envelope(
    row: DumpRow, user: DumpRow | None, account: DumpRow | None
) -> dict[str, JsonValue]:
    ids = IdentifierBag()
    ids.add("email", _str(row, "email"))
    ids.add("email", _str(row, "Alternative_email"))
    ids.add(
        "phone",
        _str(row, "contact_number"),
        region_hint=_str(row, "lk_contact_country_code"),
    )
    ids.add(
        "phone",
        _str(row, "secondary_contact_number"),
        region_hint=_str(row, "lk_secondary_contact_country_code"),
    )
    # Government IDs: NRIC is emitted as a verified identifier (handled downstream
    # by ``normalize_nric`` and the deterministic govt-ID gate). ``passport`` is
    # intentionally NOT emitted as an identifier: it has no downstream normalizer,
    # fanout cap, or govt-ID gate entry, so emitting it would create an inert,
    # un-hashed Identifier node (a sensitive-data exposure with no match value).
    # The raw passport number remains in ``raw_payload.profile`` (immutable source
    # fact). Registering ``passport`` downstream is a tracked follow-up.
    ids.add("nric", _str(row, "ic_number"), verified=True)

    address = address_from_row(_address_dict(row))
    attributes: dict[str, JsonValue] = {
        "full_name": _full_name(row),
        "first_name": _str(row, "first_name"),
        "last_name": _str(row, "last_name"),
        "gender": _str(row, "gender"),
        "dob": to_iso(_str(row, "birthday")),
        "nationality": _str(row, "lk_nationality_code"),
        "race": _str(row, "race"),
        "passport_full_name": _str(row, "passport_full_name"),
        "dive_level": _str(row, "dive_level"),
        "dives": _str(row, "dives"),
        "ssi_master_id": _str(row, "ssi_master_id"),
        "membership_id": _str(row, "membership_id"),
        "username": _str(user, "username") if user is not None else None,
        "shop_name": _str(account, "name") if account is not None else None,
    }
    raw_payload: dict[str, JsonValue] = {
        "profile": serialize_row(row),
        "user": serialize_row(user) if user is not None else {},
        "account": serialize_row(account) if account is not None else {},
    }
    return build_envelope(
        source_record_id=f"onediver-profile-{row.get('id')}",
        observed_at=to_iso_first(row.get("modified"), row.get("created")),
        identifiers=ids.items,
        attributes=attributes,
        raw_payload=raw_payload,
        addresses=[address] if address is not None else None,
        record_type="identity",
    )


def _build_relationship_envelopes(row: DumpRow) -> list[dict[str, JsonValue]]:
    """Yield up to two ``relationship`` records (kin1, kin2) for an emergency row.

    ``source_record_id`` is keyed on the emergency row's PK for uniqueness (a
    profile may have multiple emergency rows); ``linked_to_source_record_id``
    carries the ``profile_id`` FK so the engine attaches the edge to the right
    person.
    """
    emergency_id = row.get("id")
    profile_id = row.get("profile_id")
    if emergency_id is None or profile_id is None:
        # A NULL PK / FK would yield a malformed ``onediver-emergency-None-…`` /
        # ``onediver-profile-None`` link target; skip the row rather than emit one.
        return []
    out: list[dict[str, JsonValue]] = []
    for slot, first_col, last_col, num_col, rel_col in _KIN_SLOTS:
        first = _str(row, first_col)
        last = _str(row, last_col)
        full = " ".join(p for p in (first, last) if p) or None
        phone = _str(row, num_col)
        if full is None and phone is None:
            continue
        rel = _str(row, rel_col)
        ids = IdentifierBag()
        ids.add("phone", phone)
        raw_payload: dict[str, JsonValue] = {
            "emergency": serialize_row(row),
            "kin_slot": slot,
            "linked_to_source_record_id": f"onediver-profile-{profile_id}",
            "link_type": rel,
        }
        out.append(
            build_envelope(
                source_record_id=f"onediver-emergency-{emergency_id}-{slot}",
                observed_at=to_iso_first(row.get("modified"), row.get("created")),
                identifiers=ids.items,
                record_type="relationship",
                attributes={
                    "full_name": full,
                    "relationship_to_referrer": rel,
                },
                raw_payload=raw_payload,
            )
        )
    return out


def _build_sales_envelope(
    row: DumpRow, profile_id: str | None
) -> dict[str, JsonValue]:
    observed_at = to_iso_first(
        row.get("order_date"), row.get("accepted_date"), row.get("created")
    )
    raw_payload: dict[str, JsonValue] = {
        "order": {
            "source_order_id": str(row.get("id")),
            "order_no": _str(row, "order_id"),
            "ordered_at": observed_at,
            "status": _str(row, "status_code"),
            "currency": _str(row, "currency") or "SGD",
            "total_amount": coerce_float(row.get("total")),
            "raw": serialize_row(row),
        },
    }
    if profile_id is not None:
        # ``customer_link`` mirrors the standardized sales-link shape
        # (``pipeline_sales._CustomerLink``: ``identity_source_record_id`` +
        # required ``source_system_key``) used by every sibling sales source.
        raw_payload["customer_link"] = {
            "identity_source_record_id": f"onediver-profile-{profile_id}",
            "source_system_key": "onediver",
        }
    return build_envelope(
        source_record_id=f"onediver-salesorder-{row.get('id')}",
        observed_at=observed_at,
        identifiers=[],
        attributes={},
        raw_payload=raw_payload,
        record_type="sales",
    )


class OneDiverDumpConnector(SourceConnector):
    """Yields ``identity`` + ``relationship`` envelopes from an OneDiver SQL dump."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "onediver"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, ONEDIVER_TABLES)
        users_by_id = {_int(row, "id"): row for row in tables.rows("users")}
        accounts_by_id = {_int(row, "id"): row for row in tables.rows("accounts")}
        emerg_by_profile: dict[int, list[DumpRow]] = {}
        for emergency in tables.rows("profile_emergencies"):
            emerg_by_profile.setdefault(_int(emergency, "profile_id"), []).append(
                emergency
            )
        for row in sorted(tables.rows("profiles"), key=lambda r: _int(r, "id")):
            if _int(row, "is_deleted") != 0:
                continue
            if row.get("id") is None:
                continue
            user = users_by_id.get(_int(row, "user_id"))
            account = (
                accounts_by_id.get(_int(user, "account_id"))
                if user is not None
                else None
            )
            yield _build_identity_envelope(row, user, account)
            for emergency in emerg_by_profile.get(_int(row, "id"), []):
                yield from _build_relationship_envelopes(emergency)


class OneDiverSalesDumpConnector(SourceConnector):
    """Yields ``sales`` envelopes from ``sales_orders``, email-linked to profiles."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "onediver:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        tables = load_dump_tables(self._dump_path, ONEDIVER_SALES_TABLES)
        email_to_id: dict[str, str] = {}
        for row in tables.rows("profiles"):
            # Skip soft-deleted profiles: the identity connector never emits a
            # ``onediver-profile-<id>`` source record for them, so linking a
            # sales order to a deleted profile's email would produce a dangling
            # ``customer_link`` the engine cannot resolve.
            if _int(row, "is_deleted") != 0:
                continue
            # Skip NULL-PK profiles too: ``str(None)`` would index the email to
            # the literal ``'None'``, and the identity loop skips NULL-PK rows,
            # so a sales order billed to this email would dangle on
            # ``onediver-profile-None``.
            if row.get("id") is None:
                continue
            email = _str(row, "email")
            if email is not None:
                email_to_id.setdefault(email.lower(), str(row.get("id")))
        for row in sorted(tables.rows("sales_orders"), key=lambda r: _int(r, "id")):
            if row.get("id") is None:
                continue
            email = _str(row, "billing_contact_email")
            profile_id = email_to_id.get(email.lower()) if email is not None else None
            yield _build_sales_envelope(row, profile_id)