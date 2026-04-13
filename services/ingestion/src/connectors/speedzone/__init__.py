"""SpeedZone POS source connector.

Reads customer data from the SpeedZone PHP POS MySQL database (``pos_sz``)
and emits source records for the ingestion pipeline.  Connection parameters
come from environment variables (``SPEEDZONE_DB_*``); see :mod:`src.config`.

Public connectors:

- :class:`SpeedZoneConnector` (``source_key=speedzone``) — POS customers.
"""

from src.connectors.speedzone.connector import SpeedZoneConnector

__all__ = ["SpeedZoneConnector"]
