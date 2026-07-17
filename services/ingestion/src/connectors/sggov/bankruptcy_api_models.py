"""Strict wire models for the SG bankruptcy export API."""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


class BankruptcyExportItem(BaseModel):
    """One canonical bankruptcy case, optionally backed by an event."""

    model_config = ConfigDict(extra="forbid")

    case_id: int
    case_number: str
    identification_number: str | None
    person_name: str | None
    latest_document_type: str | None
    latest_document_date: date | None
    event_id: int | None
    event_type: str | None
    event_date: date | None
    trustee_name: str | None
    trustee_firm: str | None
    source_document_id: int | None
    source_url: str | None
    document_type: str | None
    document_date: date | None
    first_seen_at: datetime
    last_seen_at: datetime


class BankruptcyExportPage(BaseModel):
    """A cursor page returned by the scraper export endpoint."""

    model_config = ConfigDict(extra="forbid")

    items: list[BankruptcyExportItem]
    next_cursor: str | None
