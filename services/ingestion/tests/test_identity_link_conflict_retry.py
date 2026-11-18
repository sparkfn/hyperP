"""Bounded per-record retry for the expected identity-link CAS conflict.

``IdentityLinkConflictError`` is the expected compare-and-swap outcome when a
concurrent writer changed a locked Profile/Identifier. The record transaction
rolls back intact, so the record must be retried instead of letting the generic
task handler reject the whole delivery and lose the in-flight record plus the
remaining batch. Retries are bounded; exhaustion stays explicit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
from src.connectors.base import SourceConnector
from src.exclusions import ExclusionContext
from src.graph.client import Neo4jClient
from src.identity_links import IdentityLinkConflictError
from src.main import _ingest_all_records
from src.models import IngestResult, JsonValue, MatchDecision
from src.pipeline import IDENTITY_LINK_CONFLICT_ATTEMPTS, IngestPipeline

_RECORD_ID = "fundbox-user-1"
_INGEST_RUN_ID = "run-1"
_RESULT = IngestResult(
    source_record_id=_RECORD_ID,
    person_id="person-1",
    is_new_person=True,
    candidate_count=0,
    match_decision=MatchDecision.NO_MATCH,
    ingest_run_id=_INGEST_RUN_ID,
)


class _OneRecordConnector(SourceConnector):
    def fetch_records(self) -> Iterator[dict[str, JsonValue]]:
        yield {
            "source_record_id": _RECORD_ID,
            "observed_at": "2026-05-15T00:00:00+00:00",
            "record_hash": "hash-1",
            "identifiers": [{"type": "phone", "value": "+6599999999"}],
            "attributes": {"full_name": "Concurrent Writer"},
            "raw_payload": {},
        }

    def get_source_key(self) -> str:
        return "fundbox"


class _ConflictSession:
    """Raise the CAS conflict for the first ``failures`` write attempts."""

    def __init__(self, failures: int) -> None:
        self._failures = failures
        self.calls = 0

    def __enter__(self) -> _ConflictSession:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute_write(self, _callback: object) -> IngestResult:
        self.calls += 1
        if self.calls <= self._failures:
            raise IdentityLinkConflictError("profile changed during member attach")
        return _RESULT


class _ConflictClient:
    def __init__(self, failures: int) -> None:
        self.session_state = _ConflictSession(failures)

    def session(self) -> _ConflictSession:
        return self.session_state


def _ingest_all(client: _ConflictClient) -> tuple[int, int, int]:
    return _ingest_all_records(
        client=cast(Neo4jClient, client),
        pipeline=IngestPipeline(cast(Neo4jClient, client)),
        connector=_OneRecordConnector(),
        ingest_run_id=_INGEST_RUN_ID,
        exclusion_context=ExclusionContext(),
    )


def test_batch_retries_transient_identity_link_conflict() -> None:
    """The conflict is retried so the in-flight record succeeds and counts once."""
    client = _ConflictClient(failures=1)

    success, errors, skipped = _ingest_all(client)

    assert (success, errors, skipped) == (1, 0, 0)
    assert client.session_state.calls == 2


def test_batch_conflict_exhaustion_raises_after_bounded_attempts() -> None:
    """Exhaustion remains explicit: the record is never silently counted as success."""
    client = _ConflictClient(failures=IDENTITY_LINK_CONFLICT_ATTEMPTS)

    with pytest.raises(IdentityLinkConflictError):
        _ingest_all(client)

    assert client.session_state.calls == IDENTITY_LINK_CONFLICT_ATTEMPTS
