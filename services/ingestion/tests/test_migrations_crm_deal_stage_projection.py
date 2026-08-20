"""Regression coverage for the CRM-deal stage projection migration."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from src.graph import migrations
from src.graph.client import Neo4jClient


class _Result:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


class _Tx:
    def __init__(self, *, completed: bool = False) -> None:
        self.completed = completed
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        self.calls.append((query, params))
        if query == migrations.START_CRM_DEAL_STAGE_PROJECTION_MIGRATION:
            return _Result([{"completed_at": "2026-08-20T00:00:00Z" if self.completed else None}])
        if query == migrations.LIST_CRM_DEALS_MISSING_STAGE_PROJECTION:
            return _Result(
                [
                    {"source_record_pk": "deal-1", "raw_payload": '{"stage_id":"C2:WON"}'},
                    {"source_record_pk": "deal-2", "raw_payload": "not-json"},
                ]
            )
        return _Result([{"source_record_pk": params.get("source_record_pk")}])


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx

    def execute_write(self, work: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def test_migration_projects_stage_from_persisted_json_payload() -> None:
    tx = _Tx()

    assert migrations.migrate_crm_deal_stage_projection(cast(Neo4jClient, _Client(tx))) == 1
    assert tx.calls == [
        (migrations.START_CRM_DEAL_STAGE_PROJECTION_MIGRATION, {}),
        (migrations.LIST_CRM_DEALS_MISSING_STAGE_PROJECTION, {}),
        (
            migrations.SET_CRM_DEAL_STAGE_PROJECTION,
            {"source_record_pk": "deal-1", "crm_deal_stage_id": "C2:WON"},
        ),
        (migrations.COMPLETE_CRM_DEAL_STAGE_PROJECTION_MIGRATION, {}),
    ]


def test_completed_migration_does_not_rescan_records() -> None:
    tx = _Tx(completed=True)

    assert migrations.migrate_crm_deal_stage_projection(cast(Neo4jClient, _Client(tx))) == 0
    assert tx.calls == [(migrations.START_CRM_DEAL_STAGE_PROJECTION_MIGRATION, {})]
