"""Validated response models for the Fundbox backdoor ingestion API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import Final, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictInt, model_validator

from src.models import JsonValue

# Bounded-window identity limits, shared by the page models, the bounded client
# and the change-feed connector so every layer rejects the same oversized input.
MAX_SNAPSHOT_ID_LENGTH: Final[int] = 256
MAX_CURSOR_LENGTH: Final[int] = 2048


class PageMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None
    has_more: bool

    @model_validator(mode="after")
    def validate_continuation(self) -> PageMeta:
        if self.has_more and self.next_cursor is None:
            raise ValueError("has_more requires next_cursor")
        if not self.has_more and self.next_cursor is not None:
            raise ValueError("terminal page cannot include next_cursor")
        return self


class IngestionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[dict[str, JsonValue]]
    meta: PageMeta


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserRoot(_SourceModel):
    id: StrictInt
    email: str | None = None
    mobile_number: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BasicProfile(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    nric: str | None = None
    full_name: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    nationality: str | None = None
    race: str | None = None
    email: str | None = None
    mobile_number: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class BasicPlusProfile(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    whatsapp_phone: str | None = None
    facebook_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class Address(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    address_type: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    street: str | None = None
    building: str | None = None
    block: str | None = None
    floor: str | None = None
    unit: str | None = None
    city: str | None = None
    postal_code: str | None = None
    country: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class SocialAccount(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    provider: str | None = None
    provider_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DeviceId(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    device_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class LastLogin(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    last_logged_in: datetime | None = None


class UserComposite(_SourceModel):
    effective_updated_at: AwareDatetime
    user: UserRoot
    basic_profile: BasicProfile | None
    basic_plus_profile: BasicPlusProfile | None
    addresses: list[Address]
    social_accounts: list[SocialAccount]
    device_ids: list[DeviceId]
    last_login: LastLogin | None


class ContactRoot(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    mobile_number: str | None = None
    full_name: str | None = None
    relationship: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ContactComposite(_SourceModel):
    effective_updated_at: AwareDatetime
    contact: ContactRoot


class SalesRoot(_SourceModel):
    id: StrictInt
    user_id: StrictInt | None
    merchant_id: StrictInt | None
    merchant_staff_id: StrictInt | None
    order_no: str | None
    status: str
    total_amount: float | str | None
    total_items: int | None
    transaction_reference: str | None
    release_date: date | None
    expiry_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None


class SalesOrderItem(_SourceModel):
    id: StrictInt
    order_id: StrictInt
    merchant_product_id: StrictInt | None
    quantity: int | None
    price: float | str | None
    lta_tag: str | None
    serial_no: str | None
    created_at: datetime | None
    updated_at: datetime | None


class MerchantProductRoot(_SourceModel):
    id: StrictInt
    product_variant_id: StrictInt | None
    updated_at: datetime | None


class ProductVariantRoot(_SourceModel):
    id: StrictInt
    product_id: StrictInt | None
    sku: str | None
    name: str | None
    active: int | bool
    attributes: JsonValue
    updated_at: datetime | None


class ProductRoot(_SourceModel):
    id: StrictInt
    name: str | None
    category: str | None
    sub_category: str | None
    make: str | None
    model: str | None
    type: str | None
    sub_type: str | None
    has_serial_number: int | bool
    has_lta_tag: int | bool
    updated_at: datetime | None


class SalesItem(_SourceModel):
    order_item: SalesOrderItem
    merchant_product: MerchantProductRoot | None
    product_variant: ProductVariantRoot | None
    product: ProductRoot | None


class Merchant(_SourceModel):
    id: StrictInt
    name: str | None = None
    official_name: str | None = None
    updated_at: datetime | None = None


class CustomerUser(_SourceModel):
    id: StrictInt
    email: str | None = None
    mobile_number: str | None = None
    updated_at: datetime | None = None


class CustomerProfile(_SourceModel):
    id: StrictInt
    user_id: StrictInt
    email: str | None = None
    mobile_number: str | None = None
    nric: str | None = None
    updated_at: datetime | None = None


class Customer(_SourceModel):
    user: CustomerUser | None
    basic_profile: CustomerProfile | None


class SalesComposite(_SourceModel):
    effective_updated_at: AwareDatetime
    order: SalesRoot
    merchant: Merchant | None
    items: list[SalesItem]
    customer: Customer | None


class BoundedPageMeta(BaseModel):
    """Immutable bounded-window metadata required by the Fundbox contract."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    lower_change_version: StrictInt
    upper_change_version: StrictInt
    next_cursor: str | None = None
    terminal: bool
    cursor_expires_at: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> BoundedPageMeta:
        if not self.snapshot_id.strip() or len(self.snapshot_id) > MAX_SNAPSHOT_ID_LENGTH:
            raise ValueError("snapshot_id must be a bounded non-empty string")
        if self.lower_change_version < 0 or self.upper_change_version < 0:
            raise ValueError("change versions must be non-negative")
        if self.lower_change_version > self.upper_change_version:
            raise ValueError("lower_change_version cannot exceed upper_change_version")
        if self.next_cursor is not None and (
            not self.next_cursor.strip() or len(self.next_cursor) > MAX_CURSOR_LENGTH
        ):
            raise ValueError("next_cursor must be a bounded non-empty string")
        if self.terminal and self.next_cursor is not None:
            raise ValueError("terminal bounded page cannot include next_cursor")
        if not self.terminal and self.next_cursor is None:
            raise ValueError("non-terminal bounded page requires next_cursor")
        return self


class BoundedChange(BaseModel):
    """One effective composite upsert or explicit root tombstone."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["upsert", "tombstone"]
    change_version: StrictInt
    root_id: StrictInt
    effective_updated_at: AwareDatetime
    composite: dict[str, JsonValue] | None = None
    tombstone_reason: Literal["deleted", "ineligible"] | None = None

    @model_validator(mode="after")
    def validate_change(self) -> BoundedChange:
        if self.change_version < 0 or self.root_id < 1:
            raise ValueError("change_version and root_id must be positive")
        if self.kind == "upsert" and self.composite is None:
            raise ValueError("upsert requires a complete composite")
        if self.kind == "tombstone" and (
            self.composite is not None or self.tombstone_reason is None
        ):
            raise ValueError("tombstone requires reason and cannot include a composite")
        if self.kind == "upsert" and self.tombstone_reason is not None:
            raise ValueError("upsert cannot include tombstone_reason")
        return self


class BoundedIngestionPage(BaseModel):
    """One source-returned immutable change window page."""

    model_config = ConfigDict(extra="forbid")

    data: list[BoundedChange]
    meta: BoundedPageMeta

    @model_validator(mode="after")
    def validate_total_order(self) -> BoundedIngestionPage:
        positions = [(item.change_version, item.root_id) for item in self.data]
        if positions != sorted(positions) or len(positions) != len(set(positions)):
            raise ValueError(
                "bounded page changes must have strict (change_version, root_id) order"
            )
        if any(
            item.change_version < self.meta.lower_change_version
            or item.change_version > self.meta.upper_change_version
            for item in self.data
        ):
            raise ValueError("bounded page change falls outside frozen change window")
        return self


_VALIDATORS: dict[str, Callable[[dict[str, JsonValue]], _SourceModel]] = {
    "users": UserComposite.model_validate,
    "contacts": ContactComposite.model_validate,
    "sales": SalesComposite.model_validate,
}


def validate_source_records(
    resource: str,
    records: list[dict[str, JsonValue]],
) -> list[dict[str, JsonValue]]:
    validator = _VALIDATORS[resource]
    validated = [validator(record).model_dump(mode="json") for record in records]
    return cast(list[dict[str, JsonValue]], validated)
