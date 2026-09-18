"""Bounded descriptor discovery entry point for the Fundbox change feed.

``src.connectors.registry`` imports adapter-local ``*.bounded_descriptor``
modules and registers the descriptors they export. Keeping the export here lets
the trusted registry find the Fundbox users, contacts and sales descriptors
without importing adapter internals into shared scheduling code.
"""

from __future__ import annotations

from src.connectors.fundbox_api.bounded import DESCRIPTORS

__all__ = ["DESCRIPTORS"]
