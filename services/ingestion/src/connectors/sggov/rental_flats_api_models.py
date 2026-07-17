"""Strict wire models for the SG rental-flats export endpoint."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RentalFlatTown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    map_id: str
    map_zone: str | None


class RentalFlatRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    block_no: str
    street_name: str
    postal_code: str
    flat_type: str
    first_seen_at: datetime
    last_seen_at: datetime
    is_active: bool
    town: RentalFlatTown


class RentalFlatPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RentalFlatRow]
    total: int
    limit: int
    offset: int
    next_offset: int | None
