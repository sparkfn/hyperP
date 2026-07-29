"""Tests for the API-side Celery producer that enqueues ingestion tasks."""

from __future__ import annotations

import pytest
from src.celery_client import enqueue_ingestion_run, enqueue_match_recalculation


class _MockCelery:
    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def send_task(
        self,
        name: str,
        args: tuple[object, ...],
        kwargs: dict[str, object] | None = None,
        **options: object,
    ) -> None:
        self.sent.append((name, args, {**(kwargs or {}), **options}))


def test_enqueue_match_recalculation_sends_one_task_per_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = _MockCelery()
    monkeypatch.setattr("src.celery_client.get_celery_app", lambda: mock)

    enqueue_match_recalculation(["case-1", "case-2"])

    assert len(mock.sent) == 2
    assert mock.sent[0] == (
        "src.tasks.recalculate_pair_audit_match_task",
        ("case-1",),
        {"queue": "miscellaneous"},
    )
    assert mock.sent[1] == (
        "src.tasks.recalculate_pair_audit_match_task",
        ("case-2",),
        {"queue": "miscellaneous"},
    )


def test_enqueue_match_recalculation_noop_for_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = _MockCelery()
    monkeypatch.setattr("src.celery_client.get_celery_app", lambda: mock)

    enqueue_match_recalculation([])

    assert mock.sent == []


def test_enqueue_match_recalculation_swallows_send_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _exploding_app() -> object:
        raise RuntimeError("broker down")

    monkeypatch.setattr("src.celery_client.get_celery_app", _exploding_app)

    enqueue_match_recalculation(["case-1"])


def test_enqueue_ingestion_run_dispatches_existing_run_to_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = _MockCelery()
    monkeypatch.setattr("src.celery_client.get_celery_app", lambda: mock)

    enqueue_ingestion_run(
        "bitrix_chat",
        "backfill",
        dump_path=None,
        ingest_run_id="run-1",
    )

    assert mock.sent == [
        (
            "src.tasks.run_ingestion_task",
            ("bitrix_chat", "backfill", None),
            {"ingest_run_id": "run-1", "queue": "ingestion"},
        )
    ]
