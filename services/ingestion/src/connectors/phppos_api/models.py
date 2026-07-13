"""Strict wire models for the POS HyperP ingestion endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator


class Pagination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    next_cursor: str | None
    has_more: bool

    @model_validator(mode="after")
    def validate_cursor(self) -> Pagination:
        if self.has_more != (self.next_cursor is not None):
            raise ValueError("next_cursor must be present exactly when has_more is true")
        return self


class CustomerRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    person_id: int


class SaleLineRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    sale_id: int


class SaleRow(BaseModel):
    model_config = ConfigDict(extra="allow")

    sale_id: int
    sale_time: str
    lines: list[SaleLineRow]


class CustomerPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[CustomerRow]
    pagination: Pagination


class SalesPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data: list[SaleRow]
    pagination: Pagination
