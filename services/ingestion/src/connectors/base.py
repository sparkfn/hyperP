"""Abstract base class for source-system connectors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from src.models import JsonValue


class SourceConnector(ABC):
    """Contract that every ingestion connector must implement.

    A connector knows how to extract raw records from a single upstream
    system and yield them as plain dictionaries conforming to the common
    source-record envelope defined in the architecture doc.
    """

    @abstractmethod
    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        """Yield raw source records one at a time.

        Each dictionary should contain at minimum:
        - ``source_record_id``
        - ``observed_at``
        - ``record_hash``
        - ``identifiers`` (list)
        - ``attributes`` (dict)
        - ``raw_payload`` (dict)
        """
        ...

    @abstractmethod
    def get_source_key(self) -> str:
        """Return the ``source_key`` that identifies this upstream system.

        Must match the ``source_key`` on the corresponding ``SourceSystem``
        node in the graph.
        """
        ...
