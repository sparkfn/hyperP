"""Unit tests for the maintained Person-list summary control CLI (#489)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import cast

import pytest
from src import person_list_summary_control as control
from src.graph.queries.person_list_summary import (
    RECOMPUTE_PERSON_LIST_SUMMARY,
)


class _Rows:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __iter__(self) -> Iterator[dict[str, object]]:
        return iter(self._rows)

    def single(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None


#: A recompute row: the counter was ready, the six counts are unchanged.
_RECOMPUTE_ROW: dict[str, object] = {
    "previous_active_person_count": 40,
    "previous_high_risk_count": 7,
    "previous_high_value_count": 5,
    "previous_no_contact_count": 3,
    "previous_deals_this_month_count": 9,
    "previous_all_deals_count": 24,
    "previous_month": "2026-11",
    "previous_ready": True,
    "new_active_person_count": 40,
    "new_high_risk_count": 7,
    "new_high_value_count": 5,
    "new_no_contact_count": 3,
    "new_deals_this_month_count": 9,
    "new_all_deals_count": 24,
    "new_month": "2026-11",
    # The counter was already ready, so the backfill did not activate it.
    "activated": False,
}


class _Tx:
    """Answers the counter's recompute query."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def run(self, query: str, **_params: object) -> _Rows:
        self.calls.append(query)
        if query == RECOMPUTE_PERSON_LIST_SUMMARY:
            return _Rows([_RECOMPUTE_ROW])
        raise AssertionError(f"unexpected counter query: {query}")


class _Client:
    """A Neo4j client double that records the closed flag."""

    def __init__(self) -> None:
        self.closed = False
        self.queries: list[str] = []

    def execute_write(self, work: object) -> object:
        tx = _Tx()
        result = cast("object", work)(tx)  # type: ignore[operator]
        self.queries.extend(tx.calls)
        return result

    def close(self) -> None:
        self.closed = True


def _install(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    monkeypatch.setattr(control, "get_settings", lambda: cast(object, object()))
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)


def test_only_the_explicit_backfill_command_is_accepted() -> None:
    parser = control.build_parser()

    assert parser.parse_args(["backfill"]).command == "backfill"
    with pytest.raises(SystemExit):
        parser.parse_args(["check"])


def test_backfill_run_reports_the_published_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _Client()
    _install(monkeypatch, client)

    assert control.run(["backfill"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["command"] == "backfill"
    assert payload["active_person_count"] == 40
    assert payload["deals_this_month_count"] == 9
    assert payload["all_deals_count"] == 24
    assert payload["drift"]["active_person_count"] == 0
    assert payload["drift"]["all_deals_count"] == 0
    assert payload["month"] == "2026-11"
    assert client.closed is True
    assert RECOMPUTE_PERSON_LIST_SUMMARY in client.queries


def test_backfill_reports_an_operational_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _FailingClient:
        def __init__(self) -> None:
            self.closed = False

        def execute_write(self, work: object) -> object:
            raise RuntimeError("counter unavailable")

        def close(self) -> None:
            self.closed = True

    client = _FailingClient()
    monkeypatch.setattr(control, "get_settings", lambda: cast(object, object()))
    monkeypatch.setattr(control, "Neo4jClient", lambda _settings: client)

    assert control.run(["backfill"]) == 1

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "operational_error"}
    assert "operational_error" in captured.out
    assert client.closed is True
