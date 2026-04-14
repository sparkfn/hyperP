"""Eko POS source connector.

Reads customer data from the Eko PHP POS MySQL database.  Connection
parameters come from environment variables (``EKO_*``); set ``EKO_SSH_HOST``
to enable SSH tunnelling.  See :mod:`src.config`.

Public connectors:

- :class:`EkoConnector` (``source_key=eko``) — POS customers.
"""

from src.connectors.eko.connector import EkoConnector

__all__ = ["EkoConnector"]
