"""Fundbox source connectors.

Each connector reads a different slice of the Fundbox MySQL database and emits
source records for the ingestion pipeline. Connection parameters come from
environment variables (``FUNDBOX_DB_*``); see :mod:`src.config`.

Public connectors:

- :class:`FundboxConnector` (``source_key=fundbox_consumer_backend``) — current users.
- :class:`FundboxContactsConnector` (``source_key=fundbox_consumer_backend:contacts``) —
  emergency-contact people referenced by users.
- :class:`FundboxLegacyConnector` (``source_key=fundbox_consumer_backend:legacy``) —
  historical/migrated profiles from ``log_legacy_profiles``.
- :class:`FundboxMergedUsersConnector` (``source_key=fundbox_consumer_backend:merged``) —
  pre-existing merge lineage from the source system.
"""

from src.connectors.fundbox.contacts import FundboxContactsConnector
from src.connectors.fundbox.legacy import FundboxLegacyConnector
from src.connectors.fundbox.merged import FundboxMergedUsersConnector
from src.connectors.fundbox.sales import FundboxSalesConnector
from src.connectors.fundbox.users import FundboxConnector

__all__ = [
    "FundboxConnector",
    "FundboxContactsConnector",
    "FundboxLegacyConnector",
    "FundboxMergedUsersConnector",
    "FundboxSalesConnector",
]
