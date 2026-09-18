"""Strict wire models for the POS HyperP ingestion endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models import JsonValue


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


class BoundedCapabilities(BaseModel):
    """Source-owner guarantees required before bounded PHPPOS work is admitted."""

    model_config = ConfigDict(extra="forbid")

    effective_changes: bool
    tombstones: bool
    complete_sale_aggregates: bool
    independent_tenant_principal: bool
    replay_retention_days: int = Field(ge=30)

    @model_validator(mode="after")
    def require_safe_capabilities(self) -> BoundedCapabilities:
        if not all(
            (
                self.effective_changes,
                self.tombstones,
                self.complete_sale_aggregates,
                self.independent_tenant_principal,
            )
        ):
            raise ValueError("bounded PHPPOS capability contract is incomplete")
        return self


class BoundedWindow(BaseModel):
    """Immutable bounded-window metadata returned by the PHPPOS source."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["phppos-bounded-v1"]
    snapshot_id: str = Field(min_length=1)
    upper_change_version: str = Field(min_length=1)
    retention_until: datetime
    capabilities: BoundedCapabilities

    @model_validator(mode="after")
    def require_aware_retention(self) -> BoundedWindow:
        if self.retention_until.tzinfo is None or self.retention_until.utcoffset() is None:
            raise ValueError("retention_until must be timezone-aware")
        return self


class BoundedChange(BaseModel):
    """A complete upsert aggregate from one frozen source page."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["upsert"]
    source_id: str = Field(min_length=1)
    effective_change_version: str = Field(min_length=1)
    record: dict[str, JsonValue]


class BoundedTombstone(BaseModel):
    """An explicit source removal; absence from a page is never a tombstone."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["tombstone"]
    source_id: str = Field(min_length=1)
    effective_change_version: str = Field(min_length=1)
    removal_reason: str = Field(min_length=1)


class BoundedPage(BaseModel):
    """Strict response envelope for one immutable, tenant-bound source page."""

    model_config = ConfigDict(extra="forbid")

    contract_version: Literal["phppos-bounded-v1"]
    tenant_id: str = Field(min_length=1)
    resource: Literal["customers", "sales"]
    window: BoundedWindow
    data: list[BoundedChange | BoundedTombstone]
    pagination: Pagination

    @model_validator(mode="after")
    def require_unique_change_identities(self) -> BoundedPage:
        identities = [(change.source_id, change.effective_change_version) for change in self.data]
        if len(identities) != len(set(identities)):
            raise ValueError("bounded page repeats a source/version identity")
        return self
