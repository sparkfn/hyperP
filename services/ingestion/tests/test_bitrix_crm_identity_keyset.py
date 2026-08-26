"""Tests for the strict bounded standalone CRM client methods."""

from __future__ import annotations

import json

import httpx
import pytest
from src.connectors.bitrix_crm.identity_connector import _BitrixCrmIdentityKeysetConnector
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient
from src.connectors.bitrix_openlines.models import (
    CrmCompany,
    CrmCompanyBindingPayload,
    CrmContact,
    CrmIdentityKeysetPage,
)
from src.ingestion_config import BitrixOpenLinesConfig


def test_keyset_probe_page_and_contact_bindings_use_exact_contract() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((method, body))
        if method == "crm.contact.list" and body["order"] == {"ID": "DESC"}:
            return httpx.Response(200, json={"result": [{"ID": "90"}]})
        if method == "crm.contact.list":
            return httpx.Response(
                200,
                json={"result": [{"ID": "81", "NAME": "Ada"}, {"ID": "90", "NAME": "Bea"}]},
            )
        assert method == "crm.contact.company.items.get"
        assert body == {"id": 81}
        return httpx.Response(
            200,
            json={"result": [{"COMPANY_ID": "3", "SORT": "2", "ROLE_ID": 0, "IS_PRIMARY": "Y"}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert client.probe_crm_contact_upper_id() == 90
    page = client.list_crm_contacts_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
    bindings = client.get_contact_company_bindings("81")

    assert [record.id for record in page.records] == ["81", "90"]
    assert bindings[0].company_id == "3"
    assert calls[1][1]["filter"] == {"<=ID": 90, ">ID": 80}
    assert calls[1][1]["start"] == -1
    client.close()


def test_keyset_rejects_out_of_window_source_result() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [{"ID": "91", "NAME": "Ada"}]})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="strictly increasing"):
        client.list_crm_contacts_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
    client.close()


@pytest.mark.parametrize(
    "optional_fields", [{}, {"SORT": None, "ROLE_ID": None}, {"SORT": "", "ROLE_ID": ""}]
)
def test_contact_binding_read_normalizes_omitted_optional_items(
    optional_fields: dict[str, object],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"COMPANY_ID": "3", "IS_PRIMARY": "Y", **optional_fields}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    binding = client.get_contact_company_bindings("81")[0]
    assert binding.sort in (None, "")
    assert binding.role_id in (None, "")
    client.close()


@pytest.mark.parametrize("result", [[{"COMPANY_ID": "3"}], [{"IS_PRIMARY": "Y"}]])
def test_contact_binding_read_rejects_missing_required_items(
    result: list[dict[str, object]],
) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": result})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="omitted required fields"):
        client.get_contact_company_bindings("81")
    client.close()


class _FullCompanyPageClient:
    def __init__(self, *, returned_upper_id: int | None = None) -> None:
        self.page_calls = 0
        self.closed = False
        self.returned_upper_id = returned_upper_id

    @property
    def request_count(self) -> int:
        return self.page_calls

    def list_crm_companies_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        self.page_calls += 1
        assert greater_than_id is None
        return CrmIdentityKeysetPage(
            records=tuple(CrmCompany(id=str(value), title=None) for value in range(1, 51)),
            upper_id=(
                less_than_or_equal_to_id
                if self.returned_upper_id is None
                else self.returned_upper_id
            ),
        )

    def list_crm_contacts_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        raise AssertionError("unexpected contact call")

    def list_crm_leads_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        raise AssertionError("unexpected lead call")

    def get_contact_company_bindings(self, contact_id: str) -> tuple[CrmCompanyBindingPayload, ...]:
        raise AssertionError("unexpected binding call")

    def close(self) -> None:
        self.closed = True


def test_keyset_connector_stops_when_a_full_page_reaches_the_frozen_upper_bound() -> None:
    client = _FullCompanyPageClient()
    connector = _BitrixCrmIdentityKeysetConnector(
        client,
        BitrixOpenLinesConfig(source_instance_id="bitrix-primary"),
        kind="company",
        upper_id=50,
    )

    records = list(connector.fetch_records())

    assert len(records) == 50
    assert client.page_calls == 1
    assert connector.request_count == 1


def test_keyset_connector_rejects_changed_page_upper_bound() -> None:
    client = _FullCompanyPageClient(returned_upper_id=50)
    connector = _BitrixCrmIdentityKeysetConnector(
        client,
        BitrixOpenLinesConfig(source_instance_id="bitrix-primary"),
        kind="company",
        upper_id=51,
    )

    with pytest.raises(RuntimeError, match="changed its frozen upper bound"):
        next(connector.fetch_records())


@pytest.mark.parametrize("bad_id", ["abc", "1.5", -1, 0, True])
def test_keyset_probe_rejects_non_positive_non_numeric_ids(bad_id: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [{"ID": bad_id}]})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="omitted a numeric ID"):
        client.probe_crm_contact_upper_id()
    client.close()


def test_keyset_page_rejects_non_numeric_record_id() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": [{"ID": "contact-81"}]})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="omitted its ID"):
        client.list_crm_contacts_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
    client.close()


def test_lead_keyset_rejects_malformed_company_id_instead_of_treating_it_as_empty() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"ID": "81", "NAME": "Ada", "COMPANY_ID": "company-3"}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="invalid COMPANY_ID"):
        client.list_crm_leads_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
    client.close()


@pytest.mark.parametrize("company_id", [True, False, 1.5, [], {}])
def test_lead_keyset_rejects_non_scalar_company_id(company_id: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"ID": "81", "NAME": "Ada", "COMPANY_ID": company_id}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="invalid COMPANY_ID"):
        client.list_crm_leads_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
    client.close()


def test_lead_keyset_accepts_documented_empty_company_id_encodings() -> None:
    values: list[object] = [None, "", "  ", 0, "0"]
    responses = iter(values)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"ID": "81", "NAME": "Ada", "COMPANY_ID": next(responses)}]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    for _ in values:
        page = client.list_crm_leads_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
        assert page.records[0].company_id is None
    client.close()


def test_keyset_page_rejects_more_than_the_documented_fixed_page_size() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": [{"ID": str(value)} for value in range(1, 52)]},
        )

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="exceeded the fixed page size"):
        client.list_crm_contacts_keyset(greater_than_id=None, less_than_or_equal_to_id=100)
    client.close()


@pytest.mark.parametrize("company_id", [None, "", "  "])
def test_lead_keyset_accepts_missing_or_empty_company_id(company_id: object) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        payload = {"ID": "81", "NAME": "Ada"}
        if company_id is not ...:
            payload["COMPANY_ID"] = company_id
        return httpx.Response(200, json={"result": [payload]})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    page = client.list_crm_leads_keyset(greater_than_id=80, less_than_or_equal_to_id=90)
    assert page.records[0].company_id is None
    client.close()


class _ContactPageClient:
    def __init__(
        self,
        bindings: tuple[CrmCompanyBindingPayload, ...],
        *,
        fail_bindings: bool = False,
    ) -> None:
        self.bindings = bindings
        self.fail_bindings = fail_bindings
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    def list_crm_contacts_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        self._request_count += 1
        return CrmIdentityKeysetPage(
            records=(CrmContact(id="81", full_name="Ada"),),
            upper_id=less_than_or_equal_to_id,
        )

    def list_crm_leads_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        raise AssertionError("unexpected lead call")

    def list_crm_companies_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        raise AssertionError("unexpected company call")

    def get_contact_company_bindings(self, contact_id: str) -> tuple[CrmCompanyBindingPayload, ...]:
        self._request_count += 1
        if self.fail_bindings:
            raise RuntimeError("binding read failed")
        return self.bindings

    def close(self) -> None:
        pass


class _EmptyCompanyPageClient(_FullCompanyPageClient):
    def list_crm_companies_keyset(
        self, *, greater_than_id: int | None, less_than_or_equal_to_id: int
    ) -> CrmIdentityKeysetPage:
        self.page_calls += 1
        return CrmIdentityKeysetPage(records=(), upper_id=less_than_or_equal_to_id)


def test_keyset_connector_rejects_empty_first_page_for_positive_window() -> None:
    connector = _BitrixCrmIdentityKeysetConnector(
        _EmptyCompanyPageClient(),
        BitrixOpenLinesConfig(source_instance_id="bitrix-primary"),
        kind="company",
        upper_id=1,
    )
    with pytest.raises(RuntimeError, match="empty first page"):
        next(connector.fetch_records())


def test_contact_connector_persists_complete_empty_membership_and_counts_binding_call() -> None:
    client = _ContactPageClient(())
    connector = _BitrixCrmIdentityKeysetConnector(
        client,
        BitrixOpenLinesConfig(source_instance_id="bitrix-primary"),
        kind="contact",
        upper_id=81,
    )

    records = list(connector.fetch_records())

    assert len(records) == 1
    assert records[0]["raw_payload"]["crm_company_membership"]["bindings"] == []
    assert connector.request_count == 2


def test_contact_connector_binding_failure_occurs_before_record_publication() -> None:
    client = _ContactPageClient((), fail_bindings=True)
    connector = _BitrixCrmIdentityKeysetConnector(
        client,
        BitrixOpenLinesConfig(source_instance_id="bitrix-primary"),
        kind="contact",
        upper_id=81,
    )
    records = connector.fetch_records()

    with pytest.raises(RuntimeError, match="binding read failed"):
        next(records)
    assert connector.request_count == 2
