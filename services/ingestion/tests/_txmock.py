"""Shared test mock for Neo4j transaction recording.

Pipeline test files (sales, knows, bankruptcy, rental-flats, pair-audit) each
mock the Neo4j ``ManagedTransaction`` to assert on the queries a pipeline issues.
This base centralises the ``self.calls: list[tuple[str, dict[str, object]]]``
recording pattern so each per-file mock only overrides ``run`` to route queries
to its canned results.
"""

from __future__ import annotations


class _RecordingTx:
    """Records every ``run()`` call as ``(query, kwargs)`` in ``self.calls``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, query: str, kw: dict[str, object]) -> None:
        self.calls.append((query, dict(kw)))