"""OneDiver source connectors (dump-only).

See ``docs/superpowers/specs/2026-06-30-onediver-ingestion-design.md`` for the
source profile and the evidence behind the email-based sales link.
"""

from src.connectors.onediver.connector import (
    ONEDIVER_SALES_TABLES,
    ONEDIVER_TABLES,
    OneDiverDumpConnector,
    OneDiverSalesDumpConnector,
)

__all__ = [
    "ONEDIVER_SALES_TABLES",
    "ONEDIVER_TABLES",
    "OneDiverDumpConnector",
    "OneDiverSalesDumpConnector",
]
