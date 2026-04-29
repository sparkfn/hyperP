"""Neo4j implementation of ReportRepository."""

from __future__ import annotations

from src.graph.client import get_session
from src.graph.mappers_reports import map_report_detail, map_report_summary
from src.graph.queries import (
    CREATE_REPORT,
    DELETE_REPORT,
    GET_REPORT,
    LIST_REPORTS,
    SEED_REPORT_QUERY,
    SEED_REPORTS,
    UPDATE_REPORT,
)
from src.types_reports import ReportDetail, ReportResult, ReportSummary

from ._utils import record_to_dict


def _scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


class Neo4jReportRepository:
    async def get_all(self) -> list[ReportSummary]:
        async with get_session() as session:
            result = await session.run(LIST_REPORTS)
            records = [record_to_dict(r.keys(), list(r.values())) async for r in result]
        return [map_report_summary(rec) for rec in records]

    async def get_by_key(self, report_key: str) -> ReportDetail | None:
        async with get_session() as session:
            result = await session.run(GET_REPORT, report_key=report_key)
            record = await result.single()
        if record is None:
            return None
        return map_report_detail(record_to_dict(record.keys(), list(record.values())))

    async def create(
        self,
        report_key: str,
        display_name: str,
        description: str | None,
        category: str | None,
        cypher_query: str,
        parameters_json: str,
    ) -> None:
        async with get_session(write=True) as session:
            await session.run(
                CREATE_REPORT,
                report_key=report_key,
                display_name=display_name,
                description=description,
                category=category,
                cypher_query=cypher_query,
                parameters_json=parameters_json,
            )

    async def update(
        self,
        report_key: str,
        display_name: str,
        description: str | None,
        category: str | None,
        cypher_query: str,
        parameters_json: str,
    ) -> None:
        async with get_session(write=True) as session:
            await session.run(
                UPDATE_REPORT,
                report_key=report_key,
                display_name=display_name,
                description=description,
                category=category,
                cypher_query=cypher_query,
                parameters_json=parameters_json,
            )

    async def delete(self, report_key: str) -> int:
        async with get_session(write=True) as session:
            result = await session.run(DELETE_REPORT, report_key=report_key)
            record = await result.single()
        return int(record["deleted_count"]) if record else 0

    async def execute(
        self,
        query: str,
        params: dict[str, str | int | float | bool | None],
    ) -> ReportResult:
        async with get_session() as session:
            result = await session.run(query, **params)  # type: ignore[arg-type]
            columns: list[str] = []
            rows: list[dict[str, str | int | float | bool | None]] = []
            async for record in result:
                if not columns:
                    columns = list(record.keys())
                row: dict[str, str | int | float | bool | None] = {
                    key: _scalar(record[key]) for key in columns
                }
                rows.append(row)
        return ReportResult(columns=columns, rows=rows, row_count=len(rows))

    async def seed(self) -> list[str]:
        seeded: list[str] = []
        async with get_session(write=True) as session:
            for seed in SEED_REPORTS:
                await session.run(SEED_REPORT_QUERY, **seed)  # type: ignore[arg-type]
                seeded.append(seed["report_key"])
        return seeded
