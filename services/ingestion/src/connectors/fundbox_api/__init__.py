"""Fundbox backdoor API ingestion support."""

from src.connectors.fundbox_api.client import (
    FundboxApiClient,
    FundboxApiCredentials,
    create_fundbox_api_client,
)
from src.connectors.fundbox_api.connectors import (
    FundboxContactsApiConnector,
    FundboxSalesApiConnector,
    FundboxUsersApiConnector,
)
from src.connectors.fundbox_api.incremental import (
    FundboxIncrementalConnector,
    create_fundbox_contacts_incremental,
    create_fundbox_sales_incremental,
    create_fundbox_users_incremental,
)

__all__ = [
    "FundboxApiClient",
    "FundboxApiCredentials",
    "FundboxContactsApiConnector",
    "FundboxIncrementalConnector",
    "FundboxSalesApiConnector",
    "FundboxUsersApiConnector",
    "create_fundbox_api_client",
    "create_fundbox_contacts_incremental",
    "create_fundbox_sales_incremental",
    "create_fundbox_users_incremental",
]
