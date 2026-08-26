"""Typed values returned by the Bitrix Open Lines REST boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from src.models import JsonValue


@dataclass(frozen=True)
class OpenLineConfig:
    id: str
    line_name: str


@dataclass(frozen=True)
class CrmOwnerReference:
    owner_type: str
    owner_id: int


@dataclass(frozen=True)
class ChatReference:
    chat_id: int
    changed_at: datetime | None
    discovery: str
    activity_ids: tuple[str, ...] = ()
    crm_owner_references: tuple[CrmOwnerReference, ...] = ()
    provider_references: tuple[dict[str, JsonValue], ...] = ()
    config_id: str | None = None
    connector_id: str | None = None


@dataclass(frozen=True)
class CrmDiscoveryPage:
    references: list[ChatReference]
    next_start: int | None


@dataclass(frozen=True)
class DialogMetadata:
    chat_id: int
    config_id: str
    connector_id: str


@dataclass(frozen=True)
class OpenLineMessage:
    id: int
    author_id: int
    author_name: str
    text: str
    date: datetime
    is_agent: bool


@dataclass(frozen=True)
class CrmContact:
    """Contact or lead identity evidence returned by the Bitrix CRM API."""

    id: str
    full_name: str | None
    phones: tuple[str, ...] = ()
    emails: tuple[str, ...] = ()
    kind: str = "contact"
    observed_at: datetime | None = None
    company_id: str | None = None


@dataclass(frozen=True)
class CrmCompanyBindingPayload:
    """Transport DTO returned by ``crm.contact.company.items.get`` only."""

    company_id: object
    sort: object
    role_id: object
    is_primary: object


@dataclass(frozen=True)
class CrmIdentityKeysetPage:
    """One strict-ID keyset page for a standalone CRM kind."""

    records: tuple[CrmContact | CrmCompany, ...]
    upper_id: int


@dataclass(frozen=True)
class CrmCompany:
    """Non-Person organization reference returned by the Bitrix CRM API."""

    id: str
    title: str | None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class CrmDeal:
    """Minimal deal representation needed for person resolution and timeline."""

    id: str
    title: str
    category_id: str | None
    stage_id: str | None
    observed_at: datetime | None
    primary_contact: CrmContact | None
    contacts: tuple[CrmContact, ...]
    contact_count: int
    has_ambiguous_contacts: bool
    raw_payload: dict[str, JsonValue]


@dataclass(frozen=True)
class CrmDealCapabilityItem:
    """Minimal, read-only deal observation used by capability censuses."""

    deal_id: str
    category_id: str
    stage_id: str | None


@dataclass(frozen=True)
class CrmDealCapabilityPage:
    """One validated minimal ``crm.deal.list`` capability response page."""

    items: tuple[CrmDealCapabilityItem, ...]
    next_start: int | None
    total: int | None
    operating: float | None
    operating_reset_at: float | None


@dataclass(frozen=True)
class CrmDealStageCatalogItem:
    """One current deal-stage status from the read-only Bitrix catalog."""

    category_id: str
    stage_id: str
    semantic_id: str | None


@dataclass(frozen=True)
class CrmDealStageCatalogPage:
    """One validated ``crm.status.list`` page for a deal category."""

    items: tuple[CrmDealStageCatalogItem, ...]
    next_start: int | None
    total: int | None
    operating: float | None
    operating_reset_at: float | None


@dataclass(frozen=True)
class CrmActivity:
    """Immutable activity/timeline data from ``crm.activity.list``."""

    id: str
    owner_type: str
    owner_id: str
    history_kind: str
    subject: str | None
    observed_at: datetime | None
    start_at: datetime | None
    end_at: datetime | None
    duration_seconds: int | None
    direction: str | None
    outcome: str | None
    is_call: bool
    raw_payload: dict[str, JsonValue]


@dataclass(frozen=True)
class CrmActivityCapabilityPage:
    """One strict-ID-keyset activity page below a frozen upper boundary."""

    items: tuple[CrmActivity, ...]
    total: int | None
    operating: float | None
    operating_reset_at: float | None


def merge_chat_references(first: ChatReference, second: ChatReference) -> ChatReference:
    """Merge two discoveries without dropping their typed provenance."""
    changed_at = _latest_changed_at(first.changed_at, second.changed_at)
    discoveries = sorted(set(first.discovery.split(",") + second.discovery.split(",")))
    return ChatReference(
        chat_id=first.chat_id,
        changed_at=changed_at,
        discovery=",".join(discoveries),
        activity_ids=tuple(dict.fromkeys((*first.activity_ids, *second.activity_ids))),
        crm_owner_references=tuple(
            dict.fromkeys((*first.crm_owner_references, *second.crm_owner_references))
        ),
        provider_references=_unique_provider_references(
            (*first.provider_references, *second.provider_references)
        ),
        config_id=_first_present(first.config_id, second.config_id),
        connector_id=_first_present(first.connector_id, second.connector_id),
    )


def _latest_changed_at(first: datetime | None, second: datetime | None) -> datetime | None:
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _first_present(first: str | None, second: str | None) -> str | None:
    return first if first is not None else second


def _unique_provider_references(
    references: tuple[dict[str, JsonValue], ...],
) -> tuple[dict[str, JsonValue], ...]:
    unique: list[dict[str, JsonValue]] = []
    for reference in references:
        if reference not in unique:
            unique.append(reference)
    return tuple(unique)
