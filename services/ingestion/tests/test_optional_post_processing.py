from __future__ import annotations

from pytest import MonkeyPatch
from src import main


def test_knows_post_processing_failure_is_isolated(monkeypatch: MonkeyPatch) -> None:
    contacts_called = False

    def fail_chat(_client: object) -> int:
        raise RuntimeError("deadlock retries exhausted")

    def link_contacts(_client: object) -> int:
        nonlocal contacts_called
        contacts_called = True
        return 3

    monkeypatch.setattr(main, "materialize_knows_from_chat_relationships", fail_chat)
    monkeypatch.setattr(main, "materialize_knows_from_contacts", link_contacts)

    failures = main._materialize_optional_knows(object())

    assert failures == ["chat_relationships"]
    assert contacts_called
