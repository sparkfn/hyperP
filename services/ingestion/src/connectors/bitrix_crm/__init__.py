"""Independent Bitrix CRM deal and generic-activity source connectors."""

from src.connectors.bitrix_crm.activity_connector import BitrixCrmActivityConnector
from src.connectors.bitrix_crm.deal_connector import BitrixCrmDealConnector
from src.connectors.bitrix_crm.identity_connector import BitrixCrmIdentityConnector

__all__ = [
    "BitrixCrmActivityConnector",
    "BitrixCrmDealConnector",
    "BitrixCrmIdentityConnector",
]
