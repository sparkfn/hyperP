"""Sales domain models: Order, LineItem, Product."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SalesProduct(BaseModel):
    display_name: str | None = None
    sku: str | None = None
    category: str | None = None


class SalesLineItem(BaseModel):
    line_no: int | None = None
    quantity: float | None = None
    unit_price: float | None = None
    subtotal: float | None = None
    product: SalesProduct | None = None


class SalesOrder(BaseModel):
    order_no: str | None = None
    source_order_id: str | None = None
    order_date: str | None = None
    release_date: str | None = None
    total_amount: float | None = None
    currency: str | None = None
    source_system: str | None = None
    entity_name: str | None = None
    line_items: list[SalesLineItem] = Field(default_factory=list)
