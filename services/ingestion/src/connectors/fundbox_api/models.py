"""Validated response models for the Fundbox backdoor ingestion API."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime
from typing import cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, StrictInt, model_validator

from src.models import JsonValue


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
