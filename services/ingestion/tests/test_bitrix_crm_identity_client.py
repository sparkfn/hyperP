"""Read-only standalone Bitrix CRM list traversal contracts."""

from __future__ import annotations

import json

import httpx
from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient


def test_client_lists_contacts_leads_and_companies_with_typed_ids() -> None:
    requested_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        requested_methods.append(method)
        body = json.loads(request.content)
        assert body["order"] == {"ID": "ASC"}
        if method == "crm.contact.list":
            result = [
                {
                    "ID": "101",
                    "NAME": "Ada",
                    "LAST_NAME": "Lovelace",
                    "PHONE": [{"VALUE": "+6591234567"}],
                    "EMAIL": [{"VALUE": "ada@example.com"}],
                }
            ]
        elif method == "crm.lead.list":
            result = [{"ID": "202", "NAME": "Grace", "LAST_NAME": "Hopper"}]
        else:
            assert method == "crm.company.list"
            assert body["select"] == ["ID", "TITLE", "DATE_MODIFY", "DATE_CREATE"]
            result = [{"ID": "303", "TITLE": "Analytical Engines"}]
        return httpx.Response(200, json={"result": result})

    client = BitrixOpenLinesClient(
        base_url="https://bitrix.test/rest/hook",
        timeout_seconds=5,
        max_attempts=1,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    contacts = list(client.iter_crm_contacts())
    leads = list(client.iter_crm_leads())
    companies = list(client.iter_crm_companies())

    assert [(contact.id, contact.full_name, contact.kind) for contact in contacts] == [
        ("101", "Ada Lovelace", "contact")
    ]
    assert [(lead.id, lead.full_name, lead.kind) for lead in leads] == [
        ("202", "Grace Hopper", "lead")
    ]
    assert [(company.id, company.title) for company in companies] == [("303", "Analytical Engines")]
    assert requested_methods == ["crm.contact.list", "crm.lead.list", "crm.company.list"]
    client.close()
