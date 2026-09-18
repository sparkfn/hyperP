"""Independent Bitrix CRM deal source connector."""

from src.connectors.bitrix_crm.deal_connector import BitrixCrmDealConnector
from src.connectors.bitrix_crm.incremental import BitrixCrmDealIncrementalConnector

__all__ = [
    "BitrixCrmDealConnector",
    "BitrixCrmDealIncrementalConnector",
]
