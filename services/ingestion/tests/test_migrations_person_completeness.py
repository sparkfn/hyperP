"""Unit coverage for the person completeness-score migration and operator control."""

from __future__ import annotations

import json
from typing import cast

import pytest
from src import person_completeness_control as control
from src.graph import migrations, queries
from src.graph.client import Neo4jClient


class _Result:
    def __init__(self, row: dict[str, int | bool]) -> None:
        self._row = row

    def single(self) -> dict[str, int | bool]:
        return self._row


class _Tx:
    def __init__(self, batch_counts: list[int] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.batch_counts = list(batch_counts or [])

    def run(self, query: str, **params: object) -> _Result:
        self.calls.append((query, params))
        if query == queries.COUNT_MISSING_PERSON_COMPLETENESS_SCORES:
            return _Result({"missing_count": 3})
        if query == queries.START_PERSON_COMPLETENESS_MIGRATION:
            return _Result({"completed": False})
        if query == queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH:
            return _Result({"updated": self.batch_counts.pop(0)})
        if query == queries.COMPLETE_PERSON_COMPLETENESS_MIGRATION:
            return _Result({"missing_count": 0, "completed": True})
        raise AssertionError(f"unexpected query: {query}")


class _Client:
    def __init__(self, batch_counts: list[int] | None = None) -> None:
        self.tx = _Tx(batch_counts)

    def execute_read(self, work: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]

    def execute_write(self, work: object) -> object:
        return cast("object", work)(self.tx)  # type: ignore[operator]


def test_count_runner_reads_typed_result_count() -> None:
    client = _Client()

    assert migrations.count_missing_person_completeness_scores(cast(Neo4jClient, client)) == 3
    assert client.tx.calls == [(queries.COUNT_MISSING_PERSON_COMPLETENESS_SCORES, {})]


def test_backfill_runner_uses_restart_safe_batches_and_completion_marker() -> None:
    client = _Client([2, 1, 0])

    assert migrations.backfill_missing_person_completeness_scores(cast(Neo4jClient, client)) == 3

    assert [query for query, _params in client.tx.calls] == [
        queries.START_PERSON_COMPLETENESS_MIGRATION,
        queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH,
        queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH,
        queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH,
        queries.COMPLETE_PERSON_COMPLETENESS_MIGRATION,
    ]
    batch_params = [
        params
        for query, params in client.tx.calls
        if query == queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH
    ]
    assert (
        batch_params
        == [
            {
                "migration_key": migrations.PERSON_COMPLETENESS_MIGRATION_KEY,
                "batch_size": migrations.PERSON_COMPLETENESS_MIGRATION_BATCH_SIZE,
            }
        ]
        * 3
    )


def test_completed_startup_migration_skips_population_scan() -> None:
    client = _Client()

    def completed_start(query: str, **params: object) -> _Result:
        client.tx.calls.append((query, params))
        return _Result({"completed": True})

    client.tx.run = completed_start  # type: ignore[method-assign]

    assert migrations.backfill_missing_person_completeness_scores(cast(Neo4jClient, client)) == 0
    assert client.tx.calls == [
        (
            queries.START_PERSON_COMPLETENESS_MIGRATION,
            {
                "migration_key": migrations.PERSON_COMPLETENESS_MIGRATION_KEY,
                "force": False,
            },
        )
    ]


def test_explicit_repair_bypasses_completed_marker() -> None:
    client = _Client([1, 0])
    original_run = client.tx.run

    def completed_start(query: str, **params: object) -> _Result:
        if query == queries.START_PERSON_COMPLETENESS_MIGRATION:
            client.tx.calls.append((query, params))
            return _Result({"completed": True})
        return original_run(query, **params)

    client.tx.run = completed_start  # type: ignore[method-assign]

    assert (
        migrations.backfill_missing_person_completeness_scores(
            cast(Neo4jClient, client),
            skip_if_completed=False,
        )
        == 1
    )
    assert [query for query, _params in client.tx.calls] == [
        queries.START_PERSON_COMPLETENESS_MIGRATION,
        queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH,
        queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH,
        queries.COMPLETE_PERSON_COMPLETENESS_MIGRATION,
    ]
    assert client.tx.calls[0][1] == {
        "migration_key": migrations.PERSON_COMPLETENESS_MIGRATION_KEY,
        "force": True,
    }


def test_completeness_backfill_targets_only_list_visible_null_scores() -> None:
    query = queries.BACKFILL_MISSING_PERSON_COMPLETENESS_SCORES_BATCH

    assert "p.status <> 'merged'" in query
    assert "p.profile_completeness_score IS NULL" in query
    assert "valueType(p.profile_completeness_score) STARTS WITH 'INTEGER'" in query
    assert "valueType(p.profile_completeness_score) STARTS WITH 'FLOAT'" in query
    assert "isNaN(toFloat(p.profile_completeness_score))" in query
    assert "p.profile_completeness_score < 0.0" in query
    assert "p.profile_completeness_score > 1.0" in query
    assert "USING INDEX p:Person(person_id)" in query
    assert "p.person_id > coalesce(migration.last_person_id, '')" in query
    assert "ORDER BY p.person_id" in query
    assert "last(batch).person_id" in query
    assert "LIMIT $batch_size" in query
    assert "/ 5.0" in query
    for field in (
        "preferred_full_name",
        "preferred_phone",
        "preferred_email",
        "preferred_dob",
        "preferred_address_id",
    ):
        assert field in query
    for untouched in (
        "golden_profile_computed_at",
        "golden_profile_version",
        "analysis_input_revision",
        "survivorship_overrides",
    ):
        assert untouched not in query


def test_apply_data_migrations_runs_completeness_repair_after_record_type_backfill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    client = cast(Neo4jClient, object())
    for name in (
        "backfill_record_type_subtypes",
        "migrate_bitrix_chat_source",
        "migrate_crm_deal_stage_projection",
        "migrate_bitrix_crm_entities",
        "migrate_fundbox_source_keys",
        "migrate_source_record_lifecycle",
        "migrate_source_record_source_instances",
        "migrate_identifier_scopes",
        "migrate_projection_relationship_lifecycle",
    ):
        monkeypatch.setattr(
            migrations,
            name,
            lambda _client, *args, migration=name, **kwargs: calls.append(migration),
        )
    monkeypatch.setattr(
        migrations,
        "backfill_missing_person_completeness_scores",
        lambda _client: calls.append("person_completeness"),
    )

    migrations.apply_data_migrations(client)

    assert calls[:2] == ["backfill_record_type_subtypes", "person_completeness"]


class _ControlClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_check_reports_invariant_failure_without_writing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _ControlClient()
    monkeypatch.setattr(control, "get_settings", lambda: object())
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(control, "count_missing_person_completeness_scores", lambda _client: 2)

    assert control.run(["check"]) == 2

    assert json.loads(capsys.readouterr().out) == {
        "command": "check",
        "missing_count": 2,
        "status": "invariant_failed",
    }
    assert client.closed


def test_backfill_reports_success_after_verification(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _ControlClient()
    monkeypatch.setattr(control, "get_settings", lambda: object())
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)
    monkeypatch.setattr(
        control,
        "backfill_missing_person_completeness_scores",
        lambda _client, *, skip_if_completed: 4,
    )
    monkeypatch.setattr(control, "count_missing_person_completeness_scores", lambda _client: 0)

    assert control.run(["backfill"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "command": "backfill",
        "missing_count": 0,
        "status": "ok",
        "updated_count": 4,
    }
    assert client.closed


def test_operational_failure_returns_generic_json_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(control, "get_settings", lambda: (_ for _ in ()).throw(RuntimeError()))

    assert control.run(["check"]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "error_code": "unexpected_error",
        "status": "operational_error",
    }
    assert "RuntimeError" in captured.err
