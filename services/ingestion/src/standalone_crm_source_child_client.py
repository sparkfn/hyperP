"""Post-claim Bitrix session used exclusively by standalone CRM source children."""

from __future__ import annotations

from src.config import Settings
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_openlines.models import (
    CrmCompany,
    CrmCompanyBindingPayload,
    CrmContact,
)
from src.graph.standalone_crm_census import StandaloneCrmCensusRepository
from src.standalone_crm_census_http import (
    StandaloneCrmCensusHttpReservationHook,
    _deadline_monotonic,
)
from src.standalone_crm_source_child_runtime import StandaloneCrmSourceChildClaim


class StandaloneCrmSourceChildBitrixSession:
    """One reservation-backed client, constructed only after a durable child claim."""

    def __init__(
        self,
        client: BitrixOpenLinesClient,
        hook: StandaloneCrmCensusHttpReservationHook,
    ) -> None:
        self._client = client
        self._hook = hook

    def next_contact(self, cursor: int, frozen_upper_id: int) -> tuple[CrmContact, ...]:
        return _first_contact(
            self._client.list_crm_contacts_keyset(
                greater_than_id=_greater_than(cursor), less_than_or_equal_to_id=frozen_upper_id
            ).records,
            "contact",
        )

    def next_lead(self, cursor: int, frozen_upper_id: int) -> tuple[CrmContact, ...]:
        return _first_contact(
            self._client.list_crm_leads_keyset(
                greater_than_id=_greater_than(cursor), less_than_or_equal_to_id=frozen_upper_id
            ).records,
            "lead",
        )

    def next_company(self, cursor: int, frozen_upper_id: int) -> tuple[CrmCompany, ...]:
        records = self._client.list_crm_companies_keyset(
            greater_than_id=_greater_than(cursor), less_than_or_equal_to_id=frozen_upper_id
        ).records
        return _first_company(records)

    def contact_page_intent_id(self, cursor: int) -> str:
        return self._hook.completed_intent_id("page", cursor, None)

    def lead_page_intent_id(self, cursor: int) -> str:
        return self._hook.completed_intent_id("page", cursor, None)

    def company_page_intent_id(self, cursor: int) -> str:
        return self._hook.completed_intent_id("page", cursor, None)

    def complete_company_bindings(self, contact_id: str) -> tuple[CrmCompanyBindingPayload, ...]:
        return self._client.get_contact_company_bindings(contact_id)

    def binding_intent_id(self, contact_id: int) -> str:
        return self._hook.completed_intent_id("company_binding", contact_id, contact_id)

    def close(self) -> None:
        self._client.close()


class StandaloneCrmSourceChildBitrixSessionFactory:
    def __init__(self, settings: Settings, repository: StandaloneCrmCensusRepository) -> None:
        self._settings = settings
        self._repository = repository

    def create(self, claim: StandaloneCrmSourceChildClaim) -> StandaloneCrmSourceChildBitrixSession:
        envelope = claim.envelope
        hook = StandaloneCrmCensusHttpReservationHook(
            self._repository,
            claim.request,
            envelope.unit.census_id,
            envelope.unit.generation,
            envelope.unit.fence_token,
            envelope.unit.task_id,
            envelope.budget_authorization.attempt_deadline,
        )
        client = BitrixOpenLinesClient(
            base_url=self._settings.bitrix_openlines_api_base_url.get_secret_value(),
            timeout_seconds=self._settings.bitrix_openlines_api_timeout_seconds,
            max_attempts=self._settings.bitrix_openlines_api_max_attempts,
            request_delay_seconds=self._settings.bitrix_openlines_api_request_delay_seconds,
            max_request_count=envelope.budget_authorization.max_calls_per_attempt,
            deadline_monotonic=_deadline_monotonic(envelope.budget_authorization.attempt_deadline),
            reservation_hook=hook,
        )
        return StandaloneCrmSourceChildBitrixSession(client, hook)


def _greater_than(cursor: int) -> int | None:
    return cursor if cursor > 0 else None


def _first_contact(
    records: tuple[CrmContact | CrmCompany, ...], kind: str
) -> tuple[CrmContact, ...]:
    contacts: list[CrmContact] = []
    for record in records:
        if not isinstance(record, CrmContact) or record.kind != kind:
            raise RuntimeError("Bitrix identity source page has the wrong stream kind")
        contacts.append(record)
    return tuple(contacts[:1])


def _first_company(records: tuple[CrmContact | CrmCompany, ...]) -> tuple[CrmCompany, ...]:
    companies: list[CrmCompany] = []
    for record in records:
        if not isinstance(record, CrmCompany):
            raise RuntimeError("Bitrix company source page contained an identity record")
        companies.append(record)
    return tuple(companies[:1])
