"""Tests for dump-mode ingestion dispatch."""

from __future__ import annotations

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


class _Settings:
    log_level = "INFO"
    max_concurrent_ingestions = 1


class _NullContext:
    def __enter__(self) -> str:
        return "ok"

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False
