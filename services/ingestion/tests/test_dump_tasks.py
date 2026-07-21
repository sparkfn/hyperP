"""Tests for dump-mode ingestion dispatch."""

from __future__ import annotations

import pytest
from _test_helpers import NullContext as _NullContext
from _test_helpers import TaskSettings as _Settings
from celery.exceptions import Reject, Retry
from pytest import MonkeyPatch


def test_run_ingestion_task_passes_dump_path(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    calls: list[tuple[str, str, str | None, bool]] = []

    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda source_key: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda max_slots: _NullContext())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda source_key, mode, dump_path=None, initialize_graph=True: (
            calls.append((source_key, mode, dump_path, initialize_graph))
            or {
                "source_key": source_key,
                "mode": mode,
                "records_fetched": 0,
                "records_written": 0,
                "matches_evaluated": 0,
                "dump_path": dump_path,
            }
        ),
    )

    result = tasks.run_ingestion_task.run("whatsapp_chat", "dump", "whatsapp.sql")

    assert calls == [("whatsapp_chat", "dump", "whatsapp.sql", False)]
    assert result["dump_path"] == "whatsapp.sql"


def test_run_ingestion_task_passes_sggov_dump_paths(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    calls: list[tuple[str, str, str | None, bool]] = []

    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda source_key: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda max_slots: _NullContext())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda source_key, mode, dump_path=None, initialize_graph=True: (
            calls.append((source_key, mode, dump_path, initialize_graph))
            or {
                "source_key": source_key,
                "mode": mode,
                "records_fetched": 0,
                "records_written": 0,
                "matches_evaluated": 0,
                "dump_path": dump_path,
            }
        ),
    )

    bankruptcy = tasks.run_ingestion_task.run(
        "sgbankruptcy",
        "dump",
        "limited-100/sgbankruptcy_100.sql",
    )
    rental_flats = tasks.run_ingestion_task.run(
        "sgrentalflats",
        "dump",
        "limited-100/sgrentalflats_100.sql",
    )

    assert calls == [
        ("sgbankruptcy", "dump", "limited-100/sgbankruptcy_100.sql", False),
        ("sgrentalflats", "dump", "limited-100/sgrentalflats_100.sql", False),
    ]
    assert bankruptcy["dump_path"] == "limited-100/sgbankruptcy_100.sql"
    assert rental_flats["dump_path"] == "limited-100/sgrentalflats_100.sql"


def test_run_ingestion_task_passes_sgbankruptcy_api_mode(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    calls: list[tuple[str, str, str | None, bool]] = []
    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda source_key: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda max_slots: _NullContext())
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda source_key, mode, dump_path=None, initialize_graph=True: (
            calls.append((source_key, mode, dump_path, initialize_graph))
            or {
                "source_key": source_key,
                "mode": mode,
                "records_fetched": 0,
                "records_written": 0,
                "matches_evaluated": 0,
                "dump_path": dump_path,
            }
        ),
    )

    result = tasks.run_ingestion_task.run("sgbankruptcy", "api")

    assert calls == [("sgbankruptcy", "api", None, False)]
    assert result["mode"] == "api"


def test_run_ingestion_task_reuses_api_created_ingest_run(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    calls: list[tuple[str, str, str | None, bool, str | None]] = []
    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda source_key: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_ingestion_slot", lambda max_slots: _NullContext())
    status_checks: list[str] = []
    monkeypatch.setattr(
        tasks,
        "_get_existing_ingest_run_status",
        lambda run_id: status_checks.append(run_id) or "started",
        raising=False,
    )

    def fake_run_ingestion(
        source_key: str,
        mode: str,
        dump_path: str | None = None,
        *,
        initialize_graph: bool = True,
        existing_ingest_run_id: str | None = None,
    ) -> dict[str, object]:
        calls.append((source_key, mode, dump_path, initialize_graph, existing_ingest_run_id))
        return {
            "ingest_run_id": existing_ingest_run_id or "new-run",
            "status": "completed",
            "succeeded": 0,
            "errors": 0,
            "skipped": 0,
            "source_key": source_key,
            "mode": mode,
            "dump_path": dump_path,
        }

    monkeypatch.setattr(tasks, "run_ingestion", fake_run_ingestion)

    result = tasks.run_ingestion_task.run(
        "bitrix_chat", "backfill", None, ingest_run_id="run-1"
    )

    assert calls == [("bitrix_chat", "backfill", None, False, "run-1")]
    assert status_checks == ["run-1"]
    assert result["ingest_run_id"] == "run-1"


def test_run_ingestion_task_retries_distinct_dispatched_run_when_source_is_busy(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    finalized: list[tuple[str, str]] = []
    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())

    def source_busy(source_key: str) -> _NullContext:
        raise tasks._SourceAlreadyRunningError(source_key=source_key)

    monkeypatch.setattr(tasks, "_acquire_source_lock", source_busy)
    monkeypatch.setattr(
        tasks,
        "_finalize_dispatched_run",
        lambda ingest_run_id, status: finalized.append((ingest_run_id, status)),
        raising=False,
    )
    retry_calls: list[Exception | None] = []

    def retry(*, exc: Exception | None = None, **kwargs: object) -> Retry:
        _ = kwargs
        retry_calls.append(exc)
        raise Retry()

    monkeypatch.setattr(tasks.run_ingestion_task, "retry", retry)

    with pytest.raises(Retry):
        tasks.run_ingestion_task.run("bitrix_chat", "api", None, ingest_run_id="run-1")

    assert finalized == []
    assert len(retry_calls) == 1
    assert isinstance(retry_calls[0], tasks._SourceAlreadyRunningError)


def test_terminal_dispatched_run_redelivery_is_idempotent_noop(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "initialize_ingestion_graph", lambda: None)
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())
    monkeypatch.setattr(tasks, "_acquire_source_lock", lambda source_key: _NullContext())
    monkeypatch.setattr(
        tasks,
        "_get_existing_ingest_run_status",
        lambda run_id: "completed",
        raising=False,
    )
    monkeypatch.setattr(
        tasks,
        "_acquire_ingestion_slot",
        lambda max_slots: (_ for _ in ()).throw(AssertionError("slot acquired")),
    )
    monkeypatch.setattr(
        tasks,
        "run_ingestion",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ingestion called")),
    )

    result = tasks.run_ingestion_task.run("bitrix_chat", "api", None, ingest_run_id="run-1")

    assert result == {
        "ingest_run_id": "run-1",
        "status": "completed",
        "succeeded": 0,
        "errors": 0,
        "skipped": 1,
        "source_key": "bitrix_chat",
        "mode": "api",
        "dump_path": None,
        "entity_key": None,
    }


def test_run_ingestion_task_finalizes_api_created_run_when_setup_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEO4J_PASSWORD", "test")
    from src import tasks

    finalized: list[tuple[str, str]] = []
    monkeypatch.setattr(tasks, "setup_logging", lambda level: None)
    monkeypatch.setattr(tasks, "get_settings", lambda: _Settings())
    monkeypatch.setattr(tasks, "_acquire_init_lock", lambda: _NullContext())
    monkeypatch.setattr(
        tasks,
        "initialize_ingestion_graph",
        lambda: (_ for _ in ()).throw(RuntimeError("migration failed")),
    )
    monkeypatch.setattr(
        tasks,
        "_finalize_dispatched_run",
        lambda ingest_run_id, status: finalized.append((ingest_run_id, status)),
    )

    with pytest.raises(Reject, match="migration failed"):
        tasks.run_ingestion_task.run("bitrix_chat", "api", None, ingest_run_id="run-1")

    assert finalized == [("run-1", "failed")]
