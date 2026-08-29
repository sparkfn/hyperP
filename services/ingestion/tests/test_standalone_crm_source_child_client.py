"""One-row adapter tests for the post-claim Bitrix source-child session."""

from __future__ import annotations

from src.connectors.bitrix_openlines.models import CrmCompany, CrmContact
from src.standalone_crm_source_child_client import _first_company, _first_contact


def test_session_helpers_expose_only_the_first_strict_row_of_each_reserved_keyset_page() -> None:
    contact = CrmContact("5", "Ada", kind="contact")
    lead = CrmContact("5", "Lin", kind="lead")
    company = CrmCompany("5", "Northwind")
    contact_page = (contact, CrmContact("6", "Bea", kind="contact"))
    lead_page = (lead, CrmContact("6", "Dee", kind="lead"))
    company_page = (company, CrmCompany("6", "Tailspin"))

    assert _first_contact(contact_page, "contact") == (contact,)
    assert _first_contact(lead_page, "lead") == (lead,)
    assert _first_company(company_page) == (company,)
