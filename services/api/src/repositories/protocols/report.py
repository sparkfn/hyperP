"""Report repository protocol.

Note: the `execute` method runs a stored query string against the database.
On migration to a different backend, stored report queries must be rewritten
in the target query language (e.g. Cypher → SQL).
"""

from __future__ import annotations

from typing import Protocol

from src.types_reports import ReportDetail, ReportResult, ReportSummary


class ReportRepository(Protocol):
    async def get_all(self) -> list[ReportSummary]: ...

    async def get_by_key(self, report_key: str) -> ReportDetail | None: ...

    async def create(
        self,
        report_key: str,
        display_name: str,
        description: str | None,
        category: str | None,
        cypher_query: str,
        parameters_json: str,
    ) -> None: ...

    async def update(
        self,
        report_key: str,
        display_name: str,
        description: str | None,
        category: str | None,
        cypher_query: str,
        parameters_json: str,
    ) -> None: ...

    async def delete(self, report_key: str) -> int:
        """Returns the number of deleted nodes (0 if not found)."""
        ...

    async def execute(
        self,
        query: str,
        params: dict[str, str | int | float | bool | None],
    ) -> ReportResult: ...

    async def seed(self) -> list[str]:
        """Idempotently inserts sample reports. Returns seeded report_keys."""
        ...
