"""Shared test doubles for the ingestion task layer.

Task tests stub out the Redis-backed source lock / slot / lease holders and the
settings the task reads, so the run can be driven under a fake harness without
a broker. Centralised here so task tests in multiple files stay in sync as the
task's settings surface grows.
"""

from __future__ import annotations


class TaskSettings:
    """Minimal settings stand-in for ``run_ingestion_task``.

    Only the fields the task reads directly (``log_level`` and
    ``max_concurrent_ingestions``) are required; add new fields here as the
    task grows so every task test exercises the same shape.
    """

    log_level: str = "INFO"
    max_concurrent_ingestions: int = 1


class NullContext:
    """A context manager that yields a fixed id and swallows nothing."""

    def __enter__(self) -> str:
        return "ok"

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False
