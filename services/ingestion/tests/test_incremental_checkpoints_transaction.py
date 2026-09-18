"""Transaction-bound incremental checkpoint state and bounded durable entries."""

from __future__ import annotations

import pytest
from src.connectors.whatsadmin_api.bounded_state import WhatsAdminBoundedState
from src.connectors.whatsadmin_api.watermark import (
    bounded_entry_key,
    committed_version_key,
    session_watermark_key,
)
from src.graph.incremental_checkpoints import Neo4jCheckpointRedis


class _Result:
    def __init__(self, record: object) -> None:
        self._record = record

    def single(self) -> object:
        return self._record


class FakeTx:
    """In-transaction checkpoint store that can simulate a silently skipped write."""

    def __init__(self, store: dict[str, str]) -> None:
        self._store = store
        self.queries: list[str] = []
        self.statuses: list[object] = []
        self.skip_writes = False
        self.skip_deletes = False

    def run(self, query: str, **parameters: object) -> _Result:
        self.queries.append(query)
        key = parameters.get("checkpoint_key")
        assert isinstance(key, str)
        if "RETURN checkpoint.value AS value" in query:
            value = self._store.get(key)
            return _Result(None if value is None else {"value": value})
        if "DETACH DELETE checkpoint" in query:
            if not self.skip_deletes:
                self._store.pop(key, None)
            return _Result({"deleted": True})
        if "SET checkpoint.value" in query:
            self.statuses.append(parameters.get("status"))
            if not self.skip_writes:
                value = parameters.get("value")
                assert isinstance(value, str)
                self._store[key] = value
            return _Result({"written": True})
        raise AssertionError(f"unexpected checkpoint query: {query}")


class LegacyState:
    """Legacy state that must never be touched by a bounded transaction view."""

    def get(self, _name: str) -> object:
        raise AssertionError("bounded transaction view must not import legacy state")

    def close(self) -> None:
        raise AssertionError("bounded transaction view must not close legacy state")


class FakeClient:
    def __init__(self, tx: FakeTx) -> None:
        self._tx = tx

    def execute_read(self, work: object) -> object:
        return work(self._tx)

    def execute_write(self, work: object, **_kwargs: object) -> object:
        return work(self._tx)


def _store(
    tx: FakeTx, store: dict[str, str], *, legacy: object | None = None
) -> Neo4jCheckpointRedis:
    return Neo4jCheckpointRedis(
        FakeClient(tx),
        "whatsapp_chat",
        legacy=legacy,
        control_instance_id="bounded-control",
        reset_generation=1,
    )


def test_transaction_view_reads_its_own_writes_and_never_imports_legacy_state() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    view = _store(tx, entries, legacy=LegacyState()).bind_transaction(tx)

    assert view.get("profile_unifier:key") is None
    view.set("profile_unifier:key", "value")
    assert view.get("profile_unifier:key") == "value"
    assert entries["generation:1:profile_unifier:key"] == "value"


def test_transaction_view_rejects_a_completed_write_without_authorization() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    store = _store(tx, entries)
    watermark = session_watermark_key("eko", "ses_1")

    with pytest.raises(RuntimeError, match="terminal authorization"):
        store.bind_transaction(tx).set(watermark, "2026-09-17T06:00:00+00:00", status="completed")

    authorized = store.bind_transaction(tx, terminal_authorized=True)
    authorized.set(watermark, "2026-09-17T06:00:00+00:00", status="completed")
    assert entries[f"generation:1:{watermark}"] == "2026-09-17T06:00:00+00:00"


def test_transaction_view_requires_completed_status_for_a_watermark_key() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    store = _store(tx, entries)

    with pytest.raises(RuntimeError, match="must be written as completed"):
        store.bind_transaction(tx, terminal_authorized=True).set(
            session_watermark_key("eko", "ses_1"),
            "2026-09-17T06:00:00+00:00",
        )


def test_transaction_view_reports_a_skipped_write_as_not_durable() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    tx.skip_writes = True
    view = _store(tx, entries).bind_transaction(tx)

    with pytest.raises(RuntimeError, match="not durable"):
        view.set("profile_unifier:key", "value")


def test_transaction_view_reports_a_skipped_delete_as_not_durable() -> None:
    entries: dict[str, str] = {"generation:1:profile_unifier:key": "value"}
    tx = FakeTx(entries)
    tx.skip_deletes = True
    view = _store(tx, entries).bind_transaction(tx)

    with pytest.raises(RuntimeError, match="not durable"):
        view.delete("profile_unifier:key")


def test_transaction_view_deletes_through_the_bound_transaction() -> None:
    entries: dict[str, str] = {"generation:1:profile_unifier:key": "value"}
    tx = FakeTx(entries)
    view = _store(tx, entries).bind_transaction(tx)

    view.delete("profile_unifier:key")

    assert "generation:1:profile_unifier:key" not in entries


def test_legacy_set_status_is_still_derived_from_the_key() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    store = _store(tx, entries)

    store.set("profile_unifier:page", "resume-value")
    store.set("profile_unifier:other", "completed-value")

    assert tx.statuses == ["resume", "completed"]
    assert entries["generation:1:profile_unifier:page"] == "resume-value"
    assert entries["generation:1:profile_unifier:other"] == "completed-value"


def test_bounded_state_stages_and_reads_generation_scoped_entries() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    state = WhatsAdminBoundedState(_store(tx, entries))
    digest = "digest-1"
    name = bounded_entry_key("prepared", "eko", "ses_1", digest)

    state.stage_entry("prepared", "eko", "ses_1", digest, '{"chat_id":"chat-1"}')

    assert entries[f"generation:1:{name}"] == '{"chat_id":"chat-1"}'
    assert state.read_entry("prepared", "eko", "ses_1", digest) == '{"chat_id":"chat-1"}'
    assert state.committed_version("eko", "ses_1", "chat-1") is None

    entries[f"generation:1:{committed_version_key('eko', 'ses_1', 'chat-1')}"] = "sha256:v1"
    assert state.committed_version("eko", "ses_1", "chat-1") == "sha256:v1"


def test_bounded_state_reports_a_lost_staging_write() -> None:
    entries: dict[str, str] = {}
    tx = FakeTx(entries)
    tx.skip_writes = True
    state = WhatsAdminBoundedState(_store(tx, entries))

    with pytest.raises(RuntimeError, match="not durable"):
        state.stage_entry("bundles", "eko", "ses_1", "digest-1", "{}")
