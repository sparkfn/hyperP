"""The retired standalone CRM client path must remain unavailable."""

from src.connectors.bitrix_openlines.client import BitrixOpenLinesClient


def test_unbounded_standalone_crm_traversal_methods_are_retired() -> None:
    assert not hasattr(BitrixOpenLinesClient, "iter_crm_contacts")
    assert not hasattr(BitrixOpenLinesClient, "iter_crm_leads")
    assert not hasattr(BitrixOpenLinesClient, "iter_crm_companies")
