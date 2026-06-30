from __future__ import annotations

from fastapi import Request

from src.http_utils import client_ip


def _request(headers: list[tuple[bytes, bytes]], client: tuple[str, int] | None) -> Request:
    scope: dict[str, object] = {"type": "http", "headers": headers}
    if client is not None:
        scope["client"] = client
    return Request(scope)


def test_uses_leftmost_xff() -> None:
    req = _request([(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")], ("10.0.0.1", 0))
    assert client_ip(req) == "203.0.113.5"


def test_falls_back_to_socket_host() -> None:
    req = _request([], ("198.51.100.9", 0))
    assert client_ip(req) == "198.51.100.9"


def test_returns_none_when_unknown() -> None:
    req = _request([], None)
    assert client_ip(req) is None
