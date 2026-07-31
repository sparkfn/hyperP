"""Deferred KNOWS materialization dispatch contracts."""

from __future__ import annotations

from pytest import MonkeyPatch
from src import tasks


def test_only_relationship_sources_enqueue_deferred_knows(monkeypatch: MonkeyPatch) -> None:
    queued: list[tuple[object, ...]] = []

    def apply_async(*, args: tuple[object, ...], queue: str) -> None:
        assert queue == "lifecycle"
        queued.append(args)

    monkeypatch.setattr(tasks.materialize_knows_task, "apply_async", apply_async)

    tasks._enqueue_knows_materialization("sgbankruptcy")
    tasks._enqueue_knows_materialization("fundbox:contacts")
    tasks._enqueue_knows_materialization("bitrix_chat")

    assert queued == [("contacts",), ("chat_relationships",)]


def test_knows_phase_selection_excludes_unrelated_sources() -> None:
    assert tasks._knows_phase_for_source("sgbankruptcy") is None
    assert tasks._knows_phase_for_source("fundbox:contacts") == "contacts"
    assert tasks._knows_phase_for_source("whatsapp_chat") == "chat_relationships"
