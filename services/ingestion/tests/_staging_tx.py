"""Shared mock write transaction for pipeline tests that patch the graph writers."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

from _txmock import summary_counter_result
from neo4j import ManagedTransaction
from src.graph import queries


def tx_without_parked_row() -> ManagedTransaction:
    """A mock transaction whose parked-row lookup finds nothing.

    Persist-first staging first asks for an existing parked row of the same hash. A bare
    ``MagicMock`` answers that lookup with a truthy mock row, so the pipeline would treat the
    record as an already-staged duplicate. Every other query delegates to the maintained summary
    counter double or returns a plain mock result.
    """
    tx = MagicMock()

    def run(query: str, **_parameters: object) -> object:
        summary_result = summary_counter_result(query)
        if summary_result is not None:
            return summary_result
        result = MagicMock()
        if query == queries.FIND_PARKED_SOURCE_RECORD_BY_HASH:
            result.single.return_value = None
        return result

    tx.run.side_effect = run
    return cast(ManagedTransaction, tx)
