"""Tests for secret-safe configuration and disabled process entry-point seams."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event

import pytest
from deal_intelligence.scheduler import run as run_scheduler
from deal_intelligence.scheduler import run_idle_loop as run_scheduler_idle_loop
from deal_intelligence.scheduler import run_one_cycle as run_scheduler_one_cycle
from deal_intelligence.settings import (
    DATABASE_HOST_ENV,
    DATABASE_NAME_ENV,
    DATABASE_PASSWORD_ENV,
    DATABASE_PORT_ENV,
    DATABASE_URL_ENV,
    DATABASE_USER_ENV,
    TEST_DATABASE_URL_ENV,
    Settings,
)
from deal_intelligence.worker import run as run_worker
from deal_intelligence.worker import run_idle_loop as run_worker_idle_loop
from deal_intelligence.worker import run_one_cycle as run_worker_one_cycle
from pydantic import SecretStr


@dataclass(slots=True)
class FakeHeartbeatWriter:
    components: list[str] = field(default_factory=list)

    def write(self, component: str) -> None:
        self.components.append(component)


def test_settings_redacts_database_password_in_representation() -> None:
    settings = Settings(
        database_url=SecretStr("postgresql+psycopg://user:top-secret@example.test/deals")
    )
    assert "top-secret" not in repr(settings)
    assert settings.sqlalchemy_database_url().startswith("postgresql+psycopg://")


def test_settings_rejects_non_postgresql_url() -> None:
    with pytest.raises(ValueError):
        Settings(database_url=SecretStr("sqlite:///not-supported.db"))


def test_runtime_database_url_normalizes_a_bare_postgresql_url_without_rendering_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql://user:top-secret@example.test/deals")

    settings = Settings.from_environment()

    assert settings.sqlalchemy_database_url().startswith("postgresql+psycopg://")
    assert "top-secret" not in repr(settings)


def test_worker_and_scheduler_one_cycle_are_disabled_and_write_only_heartbeats() -> None:
    writer = FakeHeartbeatWriter()
    assert run_worker().enabled is False
    assert run_worker_one_cycle(writer).task_count == 0
    assert run_scheduler().enabled is False
    assert run_scheduler_one_cycle(writer).schedule_count == 0
    assert writer.components == ["worker", "scheduler"]


def test_idle_loops_respect_an_already_received_stop_signal() -> None:
    stop_event = Event()
    stop_event.set()
    writer = FakeHeartbeatWriter()
    run_worker_idle_loop(stop_event, writer)
    run_scheduler_idle_loop(stop_event, writer)
    assert writer.components == []


def test_test_database_environment_is_used_only_when_runtime_url_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(TEST_DATABASE_URL_ENV, "postgresql+psycopg://example.test/di_test")
    assert Settings.from_environment().sqlalchemy_database_url().endswith("/di_test")
    monkeypatch.setenv(DATABASE_URL_ENV, "postgresql+psycopg://example.test/runtime")
    assert Settings.from_environment().sqlalchemy_database_url().endswith("/runtime")


def test_separate_database_fields_build_a_psycopg_url_at_the_database_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(DATABASE_HOST_ENV, "postgres.example.test")
    monkeypatch.setenv(DATABASE_PORT_ENV, "5433")
    monkeypatch.setenv(DATABASE_NAME_ENV, "deal intelligence")
    monkeypatch.setenv(DATABASE_USER_ENV, "deal user")
    monkeypatch.setenv(DATABASE_PASSWORD_ENV, "top-secret")

    settings = Settings.from_environment()

    assert settings.sqlalchemy_database_url() == (
        "postgresql+psycopg://deal user:top-secret@postgres.example.test:5433/deal intelligence"
    )
    assert "top-secret" not in repr(settings)


def test_separate_database_password_is_required_only_at_url_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(TEST_DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(DATABASE_HOST_ENV, "postgres.example.test")
    monkeypatch.setenv(DATABASE_PORT_ENV, "5432")
    monkeypatch.setenv(DATABASE_NAME_ENV, "deals")
    monkeypatch.setenv(DATABASE_USER_ENV, "deal_user")
    monkeypatch.setenv(DATABASE_PASSWORD_ENV, "   ")

    settings = Settings.from_environment()

    with pytest.raises(ValueError, match=DATABASE_PASSWORD_ENV):
        settings.sqlalchemy_database_url()
