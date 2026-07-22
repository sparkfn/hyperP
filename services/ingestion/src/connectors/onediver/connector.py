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
from src.connectors.dumps.reader import DumpRow, iter_dump_rows
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
    # Line items + product catalogue — onediver is a scuba / water-sports
    # business with no vehicles, so every line is a non-vehicle line. The
    # connector emits them as ``non_vehicle_lines`` for Order enrichment (Task 5).
    "sales_order_items": None,
    "products": None,
}

_KIN_SLOTS = (
    ("kin1", "contact_first_name", "contact_last_name", "contact_number", "relation"),
    ("kin2", "kin2_fname", "kin2_lname", "kin2_contact", "kin2_relation"),
)
_PROFILE_BATCH_SIZE = 1000
_SALES_ORDER_BATCH_SIZE = 1000


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


def _build_onediver_line_item(
    line: DumpRow,
    products_by_id: Mapping[int, DumpRow],
    customer_nric: str | None,
) -> dict[str, JsonValue]:
    """Build one non-vehicle line item for an OneDiver sales order.

    OneDiver has no vehicles (scuba / water-sports), so every line is emitted as
    a non-vehicle line. There is no ``phppos_categories``-style mapping table, so
    ``product_type`` (a 2-char code) is left out of ``category`` and kept in
    ``attributes`` for debugging — the pipeline does not classify onediver lines.
    """
    product_id = _int(line, "product_id")
    product_row = products_by_id.get(product_id) if product_id else None
    quantity = _int(line, "quantity")
    unit_price = coerce_float(line.get("unit_price"))
    line_total = coerce_float(line.get("after_discount")) or coerce_float(line.get("price"))
    name = _str(product_row, "name") if product_row else _str(line, "name")
    sku = _str(product_row, "sku") if product_row else _str(line, "brand_sku")
    return {
        "source_line_item_id": str(line.get("id")),
        "quantity": float(quantity) if quantity else None,
        "unit_price": unit_price,
        "line_total": line_total,
        "metadata": {
            # No serial number / LTA tag / merchant in onediver's schema.
            "nric": customer_nric,
            "merchant": None,
        },
        "product": {
            "source_product_id": str(product_id) if product_id else None,
            "sku": sku,
            "name": name,
            "display_name": name,
            # ``product_type`` is a 2-char code (e.g. 's'); no name mapping
            # available, so category is None per the brief.
            "category": None,
            "manufacturer": None,
            "model": _str(product_row, "model") if product_row else None,
            "attributes": {
                "product_type": _str(line, "product_type"),
                "product_type_id": _str(line, "product_type_id"),
                "reference": _str(line, "reference"),
            },
        },
    }


def _build_sales_envelope(
    row: DumpRow,
    profile_id: str | None,
    customer_nric: str | None,
    line_rows: list[DumpRow],
    products_by_id: Mapping[int, DumpRow],
) -> dict[str, JsonValue]:
    observed_at = to_iso_first(row.get("order_date"), row.get("accepted_date"), row.get("created"))
    billing_email = _str(row, "billing_contact_email")
    # OneDiver is all non-vehicle, so every line is a non-vehicle line. Emit the
    # same list under ``line_items`` (pipeline uniformity / vehicle extraction
    # input — yields nothing for onediver) and ``non_vehicle_lines`` (Order
    # enrichment input, Task 5).
    non_vehicle_lines: list[JsonValue] = [
        _build_onediver_line_item(line, products_by_id, customer_nric)
        for line in sorted(line_rows, key=lambda r: _int(r, "id"))
    ]
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
        "line_items": non_vehicle_lines,
        "non_vehicle_lines": non_vehicle_lines,
        # Customer NRIC (``profiles.ic_number``) for the matching anti-match
        # (Task 6). Also mirrored into each line's ``metadata.nric``.
        "customer_nric": customer_nric,
        # Sale-level customer contact for the vehicle matching heuristic
        # (Task 6). OneDiver sales_orders only carries ``billing_contact_email``
        # (the sales→profile link); phone is not on the sales row, so emit an
        # empty list — the identity-record side still links via the email.
        "customer_emails": [billing_email] if billing_email is not None else [],
        "customer_phones": [],
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
        profiles = (
            row
            for row in iter_dump_rows(
                self._dump_path,
                "profiles",
                ONEDIVER_TABLES["profiles"],
            )
            if _int(row, "is_deleted") == 0 and row.get("id") is not None
        )
        for profile_batch in _row_batches(profiles, _PROFILE_BATCH_SIZE):
            yield from self._build_profile_batch(profile_batch)

    def _build_profile_batch(
        self,
        profile_batch: list[DumpRow],
    ) -> Iterator[dict[str, JsonValue]]:
        user_ids = {_int(row, "user_id") for row in profile_batch}
        users_by_id = {
            _int(row, "id"): row
            for row in iter_dump_rows(self._dump_path, "users", ONEDIVER_TABLES["users"])
            if _int(row, "id") in user_ids
        }
        account_ids = {_int(row, "account_id") for row in users_by_id.values()}
        accounts_by_id = {
            _int(row, "id"): row
            for row in iter_dump_rows(
                self._dump_path,
                "accounts",
                ONEDIVER_TABLES["accounts"],
            )
            if _int(row, "id") in account_ids
        }
        for row in profile_batch:
            user = users_by_id.get(_int(row, "user_id"))
            account = accounts_by_id.get(_int(user, "account_id")) if user is not None else None
            yield _build_identity_envelope(row, user, account)

        active_profile_ids = {_int(row, "id") for row in profile_batch}
        for emergency in iter_dump_rows(
            self._dump_path,
            "profile_emergencies",
            ONEDIVER_TABLES["profile_emergencies"],
        ):
            if _int(emergency, "profile_id") in active_profile_ids:
                yield from _build_relationship_envelopes(emergency)


class OneDiverSalesDumpConnector(SourceConnector):
    """Yields ``sales`` envelopes from ``sales_orders``, email-linked to profiles."""

    def __init__(self, dump_path: Path) -> None:
        self._dump_path = dump_path

    def get_source_key(self) -> str:
        return "onediver:sales"

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        orders = iter_dump_rows(
            self._dump_path,
            "sales_orders",
            ONEDIVER_SALES_TABLES["sales_orders"],
        )
        for order_batch in _row_batches(orders, _SALES_ORDER_BATCH_SIZE):
            yield from self._build_order_batch(order_batch)

    def _build_order_batch(
        self,
        order_batch: list[DumpRow],
    ) -> Iterator[dict[str, JsonValue]]:
        customer_emails = {
            email.lower()
            for row in order_batch
            if (email := _str(row, "billing_contact_email")) is not None
        }
        email_to_id: dict[str, str] = {}
        nric_by_profile_id: dict[str, str] = {}
        for profile in iter_dump_rows(
            self._dump_path,
            "profiles",
            ONEDIVER_SALES_TABLES["profiles"],
        ):
            email = _str(profile, "email")
            if (
                _int(profile, "is_deleted") != 0
                or profile.get("id") is None
                or email is None
                or email.lower() not in customer_emails
            ):
                continue
            profile_id = str(profile.get("id"))
            email_to_id.setdefault(email.lower(), profile_id)
            nric = _str(profile, "ic_number")
            if nric is not None:
                nric_by_profile_id[profile_id] = nric
        order_ids = {_int(row, "id") for row in order_batch if row.get("id") is not None}
        lines_by_order: dict[int, list[DumpRow]] = {}
        product_ids: set[int] = set()
        for line in iter_dump_rows(
            self._dump_path,
            "sales_order_items",
            ONEDIVER_SALES_TABLES["sales_order_items"],
        ):
            order_id = _int(line, "sales_order_id")
            if order_id not in order_ids or _int(line, "is_deleted") != 0:
                continue
            lines_by_order.setdefault(order_id, []).append(line)
            product_id = _int(line, "product_id")
            if product_id:
                product_ids.add(product_id)
        products_by_id = {
            _int(row, "id"): row
            for row in iter_dump_rows(
                self._dump_path,
                "products",
                ONEDIVER_SALES_TABLES["products"],
            )
            if row.get("id") is not None and _int(row, "id") in product_ids
        }
        for row in sorted(order_batch, key=lambda item: _int(item, "id")):
            if row.get("id") is None:
                continue
            email = _str(row, "billing_contact_email")
            matched_profile_id = email_to_id.get(email.lower()) if email is not None else None
            customer_nric = (
                nric_by_profile_id.get(matched_profile_id)
                if matched_profile_id is not None
                else None
            )
            yield _build_sales_envelope(
                row,
                matched_profile_id,
                customer_nric,
                lines_by_order.get(_int(row, "id"), []),
                products_by_id,
            )


def _row_batches(rows: Iterator[DumpRow], batch_size: int) -> Iterator[list[DumpRow]]:
    batch: list[DumpRow] = []
    for row in rows:
        batch.append(row)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch
