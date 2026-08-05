"""HyperP envelope adapters for Fundbox source-shaped API records."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

from sqlalchemy.engine import RowMapping

from src.connectors.base import SourceConnector
from src.connectors.fundbox.builders import IdentifierBag, build_envelope, serialize_row, to_iso
from src.connectors.fundbox.sales import (
    FundboxSalesConnector,
    _CustomerContact,
    _variant_to_product,
)
from src.models import JsonValue


class FundboxApiClientProtocol(Protocol):
    def iter_source(
        self, resource: str, *, updated_since: str | None = None
    ) -> Iterator[dict[str, JsonValue]]: ...

    def close(self) -> None: ...


class _Row:
    def __init__(self, values: Mapping[str, JsonValue]) -> None:
        self._values = dict(values)

    @property
    def _mapping(self) -> Mapping[str, JsonValue]:
        return self._values

    def __getattr__(self, name: str) -> JsonValue:
        try:
            return self._values[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _object(value: JsonValue | None, field: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"Fundbox API field {field!r} must be an object")
    return value


def _objects(value: JsonValue | None, field: str) -> list[dict[str, JsonValue]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"Fundbox API field {field!r} must be an object list")
    return cast(list[dict[str, JsonValue]], value)


class FundboxApiConnector(SourceConnector):
    resource: str
    root_field: str

    def __init__(
        self,
        client: FundboxApiClientProtocol,
        updated_since: str | None = None,
        previous_source_ids: set[int] | None = None,
    ) -> None:
        self._client = client
        self._updated_since = updated_since
        self._previous_source_ids = previous_source_ids
        self.latest_effective_updated_at: str | None = None
        self.current_source_ids: set[int] | None = None
        self.reconciliation_snapshot_at: str | None = None
        self.reconciliation_completed = False
        self._closed = False

    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        try:
            self.reconciliation_snapshot_at = datetime.now(UTC).isoformat()
            emitted_ids: set[int] = set()
            for composite in self._client.iter_source(
                self.resource,
                updated_since=self._updated_since,
            ):
                root_id = self._root_id(composite)
                emitted_ids.add(root_id)
                self._track_watermark(composite)
                yield self.build_record(composite)

            if self._previous_source_ids is None:
                # The initial/full request already enumerated every current
                # source ID. A second unfiltered pass cannot reconcile
                # retirements without a prior baseline and only doubles load.
                self.current_source_ids = emitted_ids
                self.reconciliation_completed = True
                return

            current_ids: set[int] = set()
            for composite in self._client.iter_source(self.resource):
                root_id = self._root_id(composite)
                current_ids.add(root_id)
                self._track_watermark(composite)
                if root_id not in emitted_ids:
                    yield self.build_record(composite)

            retired_at = datetime.now(UTC).isoformat()
            snapshot_at = self.reconciliation_snapshot_at
            assert snapshot_at is not None
            missing_ids = self._previous_source_ids - current_ids
            for root_id in sorted(missing_ids):
                yield {
                    "_retire_source_record_id": self.source_record_id(root_id),
                    "_retired_at": retired_at,
                    "_reconciliation_snapshot_at": snapshot_at,
                }
            self.current_source_ids = current_ids
            self.reconciliation_completed = True
        finally:
            self.close()

    def _root_id(self, composite: dict[str, JsonValue]) -> int:
        root = _object(composite.get(self.root_field), self.root_field)
        root_id = root.get("id")
        if type(root_id) is not int:
            raise ValueError(f"Fundbox API field {self.root_field!r}.id must be an integer")
        return root_id

    def _track_watermark(self, composite: dict[str, JsonValue]) -> None:
        effective = composite.get("effective_updated_at")
        if not isinstance(effective, str):
            return
        if self.latest_effective_updated_at is None or datetime.fromisoformat(
            effective.replace("Z", "+00:00")
        ) > datetime.fromisoformat(self.latest_effective_updated_at.replace("Z", "+00:00")):
            self.latest_effective_updated_at = effective

    def build_record(self, composite: dict[str, JsonValue]) -> dict[str, JsonValue]:
        raise NotImplementedError

    def source_record_id(self, root_id: int) -> str:
        raise NotImplementedError

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._client.close()


class FundboxUsersApiConnector(FundboxApiConnector):
    resource = "users"
    root_field = "user"

    def get_source_key(self) -> str:
        return "fundbox"

    def source_record_id(self, root_id: int) -> str:
        return f"fundbox-user-{root_id}"

    def build_record(self, composite: dict[str, JsonValue]) -> dict[str, JsonValue]:
        user = _object(composite.get("user"), "user")
        profile_value = composite.get("basic_profile")
        profile = profile_value if isinstance(profile_value, dict) else {}
        plus_value = composite.get("basic_plus_profile")
        plus = plus_value if isinstance(plus_value, dict) else {}
        addresses = [_Row(item) for item in _objects(composite.get("addresses"), "addresses")]
        socials = [
            _Row(item) for item in _objects(composite.get("social_accounts"), "social_accounts")
        ]
        devices = _objects(composite.get("device_ids"), "device_ids")
        login_value = composite.get("last_login")
        login = login_value if isinstance(login_value, dict) else {}
        last_logged_in = login.get("last_logged_in")

        identifiers = IdentifierBag()
        identifiers.add("nric", profile.get("nric"), verified=True)
        identifiers.add("email", user.get("email"), last_confirmed_at=to_iso(last_logged_in))
        identifiers.add("email", profile.get("email"), last_confirmed_at=to_iso(last_logged_in))
        identifiers.add(
            "phone", user.get("mobile_number"), last_confirmed_at=to_iso(last_logged_in)
        )
        identifiers.add(
            "phone", profile.get("mobile_number"), last_confirmed_at=to_iso(last_logged_in)
        )
        identifiers.add(
            "phone", plus.get("whatsapp_phone"), last_confirmed_at=to_iso(last_logged_in)
        )
        identifiers.add("social:facebook", plus.get("facebook_id"))
        for social in socials:
            provider = str(social._mapping.get("provider") or "").strip().lower()
            if provider:
                identifiers.add(f"social:{provider}", social._mapping.get("provider_id"))

        user_id = user.get("id")
        raw_user: dict[str, JsonValue] = {
            "user_id": user_id,
            "user_email": user.get("email"),
            "user_mobile": user.get("mobile_number"),
            "user_created_at": to_iso(user.get("created_at")),
            "user_updated_at": to_iso(user.get("updated_at")),
            "nric": profile.get("nric"),
            "full_name": profile.get("full_name"),
            "date_of_birth": to_iso(profile.get("date_of_birth")),
            "gender": profile.get("gender"),
            "nationality": profile.get("nationality"),
            "race": profile.get("race"),
            "profile_email": profile.get("email"),
            "profile_mobile": profile.get("mobile_number"),
            "whatsapp_phone": plus.get("whatsapp_phone"),
            "facebook_id": plus.get("facebook_id"),
        }
        from src.connectors.fundbox.builders import _norm_race, addresses_from_rows, format_address

        return build_envelope(
            source_record_id=f"fundbox-user-{user_id}",
            observed_at=to_iso(user.get("updated_at") or user.get("created_at")),
            identifiers=identifiers.items,
            attributes={
                "full_name": profile.get("full_name"),
                "dob": to_iso(profile.get("date_of_birth")),
                "gender": profile.get("gender"),
                "nationality": profile.get("nationality"),
                "race_ethnicity": _norm_race(profile.get("race")),
                "address": format_address(addresses[0]) if addresses else None,
            },
            raw_payload={
                "user": raw_user,
                "addresses": [serialize_row(row) for row in addresses],
                "social_accounts": [serialize_row(row) for row in socials],
                "device_ids": [item.get("device_id") for item in devices],
                "last_logged_in": to_iso(last_logged_in),
            },
            addresses=addresses_from_rows(addresses),
        )


class FundboxContactsApiConnector(FundboxApiConnector):
    resource = "contacts"
    root_field = "contact"

    def get_source_key(self) -> str:
        return "fundbox:contacts"

    def source_record_id(self, root_id: int) -> str:
        return f"fundbox-contact-{root_id}"

    def build_record(self, composite: dict[str, JsonValue]) -> dict[str, JsonValue]:
        contact = _object(composite.get("contact"), "contact")
        identifiers = IdentifierBag()
        identifiers.add("phone", contact.get("mobile_number"))
        return build_envelope(
            source_record_id=f"fundbox-contact-{contact.get('id')}",
            observed_at=to_iso(contact.get("updated_at") or contact.get("created_at")),
            identifiers=identifiers.items,
            record_type="relationship",
            attributes={
                "full_name": contact.get("full_name"),
                "relationship_to_referrer": contact.get("relationship"),
            },
            raw_payload={
                "contact": serialize_row(contact),
                "linked_to_source_record_id": (f"fundbox-user-{contact.get('user_id')}"),
                "link_type": contact.get("relationship"),
            },
        )


class FundboxSalesApiConnector(FundboxApiConnector):
    resource = "sales"
    root_field = "order"

    def get_source_key(self) -> str:
        return "fundbox:sales"

    def source_record_id(self, root_id: int) -> str:
        return f"fundbox-order-{root_id}"

    def build_record(self, composite: dict[str, JsonValue]) -> dict[str, JsonValue]:
        order = _object(composite.get("order"), "order")
        merchant_value = composite.get("merchant")
        merchant = merchant_value if isinstance(merchant_value, dict) else {}
        item_composites = _objects(composite.get("items"), "items")
        line_rows: list[_Row] = []
        product_info: dict[int, dict[str, JsonValue]] = {}
        for item_composite in item_composites:
            item = _object(item_composite.get("order_item"), "items.order_item")
            line_rows.append(_Row(item))
            merchant_product_value = item_composite.get("merchant_product")
            merchant_product = (
                merchant_product_value if isinstance(merchant_product_value, dict) else {}
            )
            variant_value = item_composite.get("product_variant")
            variant = dict(variant_value) if isinstance(variant_value, dict) else {}
            if "attributes" in variant:
                variant["attributes"] = _decode_json_value(variant["attributes"])
            product_value = item_composite.get("product")
            product = _Row(product_value) if isinstance(product_value, dict) else None
            merchant_product_id = merchant_product.get("id")
            if isinstance(merchant_product_id, int) and variant:
                product_info[merchant_product_id] = _variant_to_product(
                    cast(RowMapping, _Row(variant)),
                    cast(RowMapping | None, product),
                )

        customer_value = composite.get("customer")
        customer = customer_value if isinstance(customer_value, dict) else {}
        customer_user_value = customer.get("user")
        customer_user = customer_user_value if isinstance(customer_user_value, dict) else {}
        customer_profile_value = customer.get("basic_profile")
        customer_profile = (
            customer_profile_value if isinstance(customer_profile_value, dict) else {}
        )
        emails = _unique_strings([customer_user.get("email"), customer_profile.get("email")])
        phones = _unique_strings(
            [customer_user.get("mobile_number"), customer_profile.get("mobile_number")]
        )
        customer_contact: _CustomerContact = {
            "customer_emails": emails,
            "customer_phones": phones,
            "customer_nric": _optional_string(customer_profile.get("nric")),
        }
        merchant_id = order.get("merchant_id")
        merchant_name_value = merchant.get("name") or merchant.get("official_name")
        merchant_names = (
            {merchant_id: str(merchant_name_value)}
            if isinstance(merchant_id, int) and merchant_name_value is not None
            else {}
        )
        return FundboxSalesConnector()._build_one(
            cast(RowMapping, _Row(order)),
            cast(list[RowMapping], line_rows),
            merchant_names,
            product_info,
            customer_contact,
        )


def _unique_strings(values: list[JsonValue | None]) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
    return result


def _optional_string(value: JsonValue | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _decode_json_value(value: JsonValue) -> JsonValue:
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Fundbox product variant attributes must contain valid JSON") from exc
    return cast(JsonValue, parsed)
