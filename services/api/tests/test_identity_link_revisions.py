from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient
from neo4j import AsyncManagedTransaction
from src.auth.deps import OAuthClientUser, get_current_user_or_oauth_client
from src.graph.queries.identity_link_revisions import (
    APPEND_IDENTITY_LINK_REVISIONS,
    GET_AFFECTED_IDENTITY_LINK_HEADS,
    LIST_IDENTITY_LINK_EVENTS,
    LIST_IDENTITY_LINK_SNAPSHOT,
)
from src.identity_link_cursors import (
    IdentityLinkCursor,
    decode_identity_link_cursor,
    encode_identity_link_cursor,
)
from src.identity_link_revisions import (
    append_identity_link_revisions,
    append_merge_affected_revisions,
)
from src.identity_link_types import (
    IdentityLinkDesiredRevision,
    IdentityLinkRevision,
    identity_link_key,
)
from src.oauth2_app import build_oauth2_app
from src.repositories.deps import get_identity_link_revision_repo
from src.repositories.neo4j.identity_link_revision import (
    IdentityLinkRevisionGapError,
    Neo4jIdentityLinkRevisionRepository,
)
from src.route_catalog import MCP_OPERATION_EXCLUSIONS
from src.routes.identity_link_revisions import (
    list_identity_link_events,
    list_identity_link_snapshot,
)
from starlette.requests import Request


def _revision(*, global_revision: int, resolution_revision: int) -> IdentityLinkRevision:
    return IdentityLinkRevision(
        event_id=f"event-{global_revision}",
        global_revision=global_revision,
        source_system="bitrix_chat",
        source_instance_id="portal-1",
        source_entity_type="contact",
        source_entity_id="contact-1",
        identity_policy_version="crm_contact_identity_v1",
        link_status="resolved",
        hyperp_person_id="person-1",
        resolution_kind="automatic_activation",
        resolution_revision=resolution_revision,
        effective_at=datetime(2026, 8, 26),
    )


class _Repo:
    def __init__(
        self, items: list[IdentityLinkRevision], revision: int, ready: bool = True
    ) -> None:
        self.items = items
        self.revision = revision
        self.ready = ready

    async def current_revision_and_ready(self) -> tuple[int, bool]:
        return self.revision, self.ready

    async def event_page(self, after: int, through: int, limit: int) -> list[IdentityLinkRevision]:
        return [item for item in self.items if after < item.global_revision <= through][:limit]

    async def snapshot_page(
        self, revision: int, after: str, limit: int
    ) -> tuple[list[IdentityLinkRevision], str | None]:
        return self.items[:limit], None


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def test_public_revision_has_exact_safe_fields() -> None:
    assert set(_revision(global_revision=1, resolution_revision=1).model_dump()) == {
        "event_id",
        "global_revision",
        "source_system",
        "source_instance_id",
        "source_entity_type",
        "source_entity_id",
        "identity_policy_version",
        "link_status",
        "hyperp_person_id",
        "resolution_kind",
        "resolution_revision",
        "effective_at",
        "match_decision_id",
        "review_case_id",
        "supersedes_event_id",
    }
    text = LIST_IDENTITY_LINK_EVENTS + LIST_IDENTITY_LINK_SNAPSHOT
    for forbidden in ("raw_payload", "normalized_payload", "candidate", "confidence", "reasons"):
        assert forbidden not in text


@pytest.mark.parametrize(
    "status", ["unresolved", "pending_review", "blocked", "rejected", "retired"]
)
def test_non_resolved_status_rejects_person(status: str) -> None:
    data = _revision(global_revision=1, resolution_revision=1).model_dump()
    data["link_status"] = status
    with pytest.raises(ValueError):
        IdentityLinkRevision.model_validate(data)


def test_cursor_is_versioned_kind_specific_and_non_advancing_is_rejected() -> None:
    cursor = encode_identity_link_cursor(
        IdentityLinkCursor(kind="events", after_revision=4, through_revision=8)
    )
    assert decode_identity_link_cursor(cursor, "events").through_revision == 8
    with pytest.raises(ValueError):
        decode_identity_link_cursor(cursor, "snapshot")
    with pytest.raises(ValueError):
        encode_identity_link_cursor(
            IdentityLinkCursor(kind="events", after_revision=8, through_revision=8)
        )


def test_cursor_is_authenticated_opaque_and_tamper_evident() -> None:
    cursor = encode_identity_link_cursor(
        IdentityLinkCursor(
            kind="snapshot", snapshot_revision=8, after_link_key="ilk1:private-link-key"
        )
    )
    assert "ilk1" not in cursor
    assert "private-link-key" not in cursor
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    with pytest.raises(ValueError):
        decode_identity_link_cursor(tampered, "snapshot")


@pytest.mark.asyncio
async def test_event_and_snapshot_pages_return_fixed_bounds() -> None:
    repo = _Repo([_revision(global_revision=1, resolution_revision=1)], revision=7)
    events = await list_identity_link_events(_request(), after_revision=0, limit=50, repo=repo)
    snapshot = await list_identity_link_snapshot(_request(), limit=50, repo=repo)
    assert events.through_revision == 7
    assert snapshot.snapshot_revision == 7
    assert events.meta.next_cursor is None


class _AsyncResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def __aiter__(self) -> AsyncIterator[dict[str, object]]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[dict[str, object]]:
        for row in self._rows:
            yield row


class _AsyncSession:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    async def __aenter__(self) -> _AsyncSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def run(self, *args: object, **kwargs: object) -> _AsyncResult:
        _ = args, kwargs
        return _AsyncResult(self._rows)


def _revision_row(global_revision: int) -> dict[str, object]:
    return {
        "revision": _revision(global_revision=global_revision, resolution_revision=1).model_dump()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after", "through", "limit", "rows"),
    [
        (0, 1, 50, []),
        (0, 3, 50, [_revision_row(2), _revision_row(3)]),
        (0, 3, 50, [_revision_row(1), _revision_row(3)]),
        (0, 10, 50, [_revision_row(value) for value in range(1, 6)]),
    ],
)
async def test_event_gap_detection_rejects_empty_first_internal_and_missing_tail(
    after: int, through: int, limit: int, rows: list[dict[str, object]]
) -> None:
    with patch(
        "src.repositories.neo4j.identity_link_revision.get_session",
        return_value=_AsyncSession(rows),
    ):
        with pytest.raises(IdentityLinkRevisionGapError):
            await Neo4jIdentityLinkRevisionRepository().event_page(after, through, limit)


@pytest.mark.asyncio
async def test_event_page_allows_caught_up_poll_and_rejects_ahead_checkpoint() -> None:
    repository = Neo4jIdentityLinkRevisionRepository()
    assert await repository.event_page(7, 7, 50) == []
    with pytest.raises(IdentityLinkRevisionGapError):
        await repository.event_page(8, 7, 50)


@pytest.mark.asyncio
async def test_snapshot_corruption_is_not_silently_omitted() -> None:
    rows = [{"link_key": "ilk1:broken", "revision": None}]
    with patch(
        "src.repositories.neo4j.identity_link_revision.get_session",
        return_value=_AsyncSession(rows),
    ):
        with pytest.raises(RuntimeError, match="snapshot corruption"):
            await Neo4jIdentityLinkRevisionRepository().snapshot_page(7, "", 50)


@pytest.mark.asyncio
@pytest.mark.parametrize("later_status", ["pending_review", "rejected", "retired"])
async def test_merge_excludes_non_resolved_or_later_lifecycle_heads(later_status: str) -> None:
    """The Cypher filter uses the current head revision, not source provenance alone."""
    transaction = _AffectedHeadTransaction([])
    assert (
        await append_merge_affected_revisions(
            cast(AsyncManagedTransaction, transaction),
            merge_event_id="merge-1",
            absorbed_person_id="absorbed-person",
            survivor_person_id="survivor-person",
            resolution_kind="person_merge",
            cause_prefix="person-merge:merge-1",
            effective_at="2026-08-26T00:00:00+00:00",
        )
        == []
    )
    assert [query for query, _ in transaction.calls] == [GET_AFFECTED_IDENTITY_LINK_HEADS]
    assert later_status != "resolved"


@pytest.mark.asyncio
async def test_unmerge_excludes_head_retired_after_original_merge() -> None:
    transaction = _AffectedHeadTransaction([])
    assert (
        await append_merge_affected_revisions(
            cast(AsyncManagedTransaction, transaction),
            merge_event_id="merge-1",
            absorbed_person_id="absorbed-person",
            survivor_person_id=None,
            resolution_kind="person_unmerge",
            cause_prefix="person-unmerge:unmerge-audit-1",
            effective_at="2026-08-26T00:00:00+00:00",
        )
        == []
    )
    assert [query for query, _ in transaction.calls] == [GET_AFFECTED_IDENTITY_LINK_HEADS]


@pytest.mark.asyncio
async def test_merge_append_uses_one_contiguous_batch_for_eligible_heads() -> None:
    policy = "crm_contact_identity_v1"
    link_key = identity_link_key("bitrix_chat", "portal-1", "contact", "contact-1", policy)
    transaction = _AffectedHeadTransaction(
        [
            {
                "source_system": "bitrix_chat",
                "source_instance_id": "portal-1",
                "source_entity_type": "contact",
                "source_entity_id": "contact-1",
                "identity_policy_version": policy,
                "link_key": link_key,
                "match_decision_id": None,
                "review_case_id": None,
            }
        ]
    )
    await append_merge_affected_revisions(
        cast(AsyncManagedTransaction, transaction),
        merge_event_id="merge-1",
        absorbed_person_id="absorbed-person",
        survivor_person_id="survivor-person",
        resolution_kind="person_merge",
        cause_prefix="person-merge:merge-1",
        effective_at="2026-08-26T00:00:00+00:00",
    )
    assert len(transaction.calls) == 2
    query, params = transaction.calls[0]
    assert query == GET_AFFECTED_IDENTITY_LINK_HEADS
    assert params == {
        "merge_event_id": "merge-1",
        "absorbed_person_id": "absorbed-person",
        "operation": "merge",
        "merge_cause_prefix": "person-merge:merge-1:",
    }
    append_query, append_params = transaction.calls[1]
    assert append_query == APPEND_IDENTITY_LINK_REVISIONS
    rows = append_params["rows"]
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["cause_key"] == f"person-merge:merge-1:{link_key}"


def test_append_and_merge_queries_preserve_atomic_stream_invariants() -> None:
    assert "$skip_existing_heads" in APPEND_IDENTITY_LINK_REVISIONS
    lock_at = APPEND_IDENTITY_LINK_REVISIONS.index("SET counter.updated_at = datetime()")
    duplicate_read_at = APPEND_IDENTITY_LINK_REVISIONS.index("OPTIONAL MATCH (existing:")
    assert lock_at < duplicate_read_at
    assert (
        "counter.current_revision = counter.current_revision + size(rows)"
        in APPEND_IDENTITY_LINK_REVISIONS
    )
    assert "CASE WHEN size(rows) = 0 THEN []" in APPEND_IDENTITY_LINK_REVISIONS
    assert "UNWIND CASE WHEN size(rows) = 0" in APPEND_IDENTITY_LINK_REVISIONS
    assert "supersedes_event_id: previous_head.latest_event_id" in APPEND_IDENTITY_LINK_REVISIONS
    assert "AFFECTED_RECORD" in GET_AFFECTED_IDENTITY_LINK_HEADS
    assert "revision.link_status = 'resolved'" in GET_AFFECTED_IDENTITY_LINK_HEADS
    assert "revision.hyperp_person_id = $absorbed_person_id" in GET_AFFECTED_IDENTITY_LINK_HEADS
    assert (
        "revision.cause_key = $merge_cause_prefix + head.link_key"
        in GET_AFFECTED_IDENTITY_LINK_HEADS
    )
    assert (
        "OPTIONAL MATCH (revision:IdentityLinkRevision {link_key: head.link_key})"
        in LIST_IDENTITY_LINK_SNAPSHOT
    )


class _AffectedHeadTransaction:
    def __init__(self, affected_rows: list[dict[str, object]]) -> None:
        self.affected_rows = affected_rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def run(self, query: str, **params: object) -> _AsyncResult:
        self.calls.append((query, params))
        if query == GET_AFFECTED_IDENTITY_LINK_HEADS:
            return _AsyncResult(self.affected_rows)
        assert query == APPEND_IDENTITY_LINK_REVISIONS
        return _AsyncResult([])


class _AppendTransaction:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(self, query: str, **params: object) -> _AsyncResult:
        assert query == APPEND_IDENTITY_LINK_REVISIONS
        self.calls.append(params)
        return _AsyncResult([])


def _desired(cause_key: str) -> IdentityLinkDesiredRevision:
    return IdentityLinkDesiredRevision(
        source_system="bitrix_chat",
        source_instance_id="portal-1",
        source_entity_type="contact",
        source_entity_id="contact-1",
        identity_policy_version="crm_contact_identity_v1",
        link_status="unresolved",
        hyperp_person_id=None,
        resolution_kind="baseline",
        effective_at="2026-08-26T00:00:00+00:00",
        cause_key=cause_key,
    )


@pytest.mark.asyncio
async def test_append_deduplicates_causes_and_does_not_run_for_empty_batches() -> None:
    transaction = _AppendTransaction()
    assert (
        await append_identity_link_revisions(cast(AsyncManagedTransaction, transaction), []) == []
    )
    assert transaction.calls == []
    assert (
        await append_identity_link_revisions(
            cast(AsyncManagedTransaction, transaction), [_desired("cause-1"), _desired("cause-1")]
        )
        == []
    )
    assert len(transaction.calls) == 1
    rows = transaction.calls[0]["rows"]
    assert isinstance(rows, list) and len(rows) == 1


def test_static_openapi_declares_both_identity_link_machine_operations() -> None:
    document = yaml.safe_load(
        Path("docs/profile-unifier-openapi-3.1.yaml").read_text(encoding="utf-8")
    )
    paths = document["paths"]
    events = paths["/oauth2/v1/identity-links/events"]["get"]
    snapshot = paths["/oauth2/v1/identity-links/snapshot"]["get"]
    assert events["operationId"] == "list_identity_link_events_machine"
    assert snapshot["operationId"] == "list_identity_link_snapshot_machine"
    for operation in (events, snapshot):
        assert operation["security"] == [{"bearerAuth": ["identity-links:read"]}]
        assert {"200", "400", "401", "403", "503"} <= set(operation["responses"])


def test_machine_operations_are_explicitly_excluded_from_mcp() -> None:
    exclusions = {item.operation_id: item for item in MCP_OPERATION_EXCLUSIONS}
    assert set(exclusions) == {
        "list_identity_link_events_machine",
        "list_identity_link_snapshot_machine",
    }
    assert all(item.reason.strip() for item in exclusions.values())


class _AuthorizedRepo(_Repo):
    pass


async def _oauth_reader() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:reader",
        google_sub="reader",
        role="admin",
        entity_key=None,
        display_name="Reader",
        source="oauth_client",
        client_id="reader",
        key_scopes=["identity-links:read"],
    )


async def _oauth_wrong_scope() -> OAuthClientUser:
    user = await _oauth_reader()
    return user.model_copy(update={"key_scopes": ["persons:read"]})


async def _oauth_entity_scoped() -> OAuthClientUser:
    user = await _oauth_reader()
    return user.model_copy(update={"entity_key": "tenant-a"})


async def _human() -> object:
    from src.auth.models import AuthUser

    return AuthUser(email="human@example.com", google_sub="human", role="admin", entity_key=None)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (_human, "forbidden"),
        (_oauth_wrong_scope, "forbidden"),
        (_oauth_entity_scoped, "forbidden_entity_scope"),
    ],
)
def test_oauth_stream_rejects_human_wrong_scope_and_entity_scoped_clients(
    override: object, expected: str
) -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = override
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: _AuthorizedRepo([], 0)
    response = TestClient(app).get("/identity-links/events")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == expected


def test_oauth_stream_routes_are_oauth_only_and_use_frozen_bounds() -> None:
    app = build_oauth2_app()
    repo = _AuthorizedRepo([_revision(global_revision=1, resolution_revision=1)], revision=7)
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_reader
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: repo
    client = TestClient(app)
    assert (
        client.get("/identity-links/events", params={"after_revision": 0}).json()[
            "through_revision"
        ]
        == 7
    )
    assert client.get("/identity-links/snapshot").json()["snapshot_revision"] == 7
    assert client.get("/v1/identity-links/events").status_code == 404


def test_oauth_events_allow_caught_up_poll_and_reject_ahead_checkpoint() -> None:
    class _CheckpointRepo(_AuthorizedRepo):
        async def event_page(
            self, after: int, through: int, limit: int
        ) -> list[IdentityLinkRevision]:
            _ = limit
            if after > through:
                raise IdentityLinkRevisionGapError("checkpoint ahead")
            return []

    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_reader
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: _CheckpointRepo([], 7)
    client = TestClient(app)
    caught_up = client.get("/identity-links/events", params={"after_revision": 7})
    assert caught_up.status_code == 200
    assert caught_up.json()["data"] == []
    ahead = client.get("/identity-links/events", params={"after_revision": 8})
    assert ahead.status_code == 503
    assert ahead.json()["error"]["code"] == "identity_link_revision_gap"


def test_oauth_snapshot_surfaces_corruption_as_stream_unavailable() -> None:
    class _CorruptRepo(_AuthorizedRepo):
        async def snapshot_page(
            self, revision: int, after: str, limit: int
        ) -> tuple[list[IdentityLinkRevision], str | None]:
            _ = revision, after, limit
            raise RuntimeError("identity-link snapshot corruption")

    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_reader
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: _CorruptRepo([], 2)
    response = TestClient(app).get("/identity-links/snapshot")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_link_stream_unavailable"


def test_oauth_stream_readiness_cursor_conflict_and_wrong_kind_are_errors() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_reader
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: _AuthorizedRepo(
        [], 2, ready=False
    )
    client = TestClient(app)
    assert (
        client.get("/identity-links/snapshot").json()["error"]["code"]
        == "identity_link_snapshot_not_ready"
    )
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: _AuthorizedRepo([], 2)
    events_cursor = encode_identity_link_cursor(
        IdentityLinkCursor(kind="events", after_revision=0, through_revision=2)
    )
    assert (
        client.get(
            "/identity-links/events", params={"cursor": events_cursor, "after_revision": 0}
        ).status_code
        == 400
    )
    assert (
        client.get("/identity-links/snapshot", params={"cursor": events_cursor}).status_code == 400
    )


def test_oauth_cursor_pages_revalidate_bounds_readiness_and_fixed_snapshot() -> None:
    class _TrackingRepo(_AuthorizedRepo):
        def __init__(self) -> None:
            super().__init__([_revision(global_revision=5, resolution_revision=1)], revision=8)
            self.snapshot_calls: list[tuple[int, str, int]] = []

        async def snapshot_page(
            self, revision: int, after: str, limit: int
        ) -> tuple[list[IdentityLinkRevision], str | None]:
            self.snapshot_calls.append((revision, after, limit))
            return self.items, None

    app = build_oauth2_app()
    repo = _TrackingRepo()
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_reader
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: repo
    client = TestClient(app)

    forged_event_bound = encode_identity_link_cursor(
        IdentityLinkCursor(kind="events", after_revision=1, through_revision=9)
    )
    event_response = client.get("/identity-links/events", params={"cursor": forged_event_bound})
    assert event_response.status_code == 400
    assert event_response.json()["error"]["code"] == "invalid_cursor"

    future_snapshot_bound = encode_identity_link_cursor(
        IdentityLinkCursor(kind="snapshot", snapshot_revision=9, after_link_key="ilk1:private")
    )
    future_response = client.get(
        "/identity-links/snapshot", params={"cursor": future_snapshot_bound}
    )
    assert future_response.status_code == 400
    assert future_response.json()["error"]["code"] == "invalid_cursor"

    fixed_cursor = encode_identity_link_cursor(
        IdentityLinkCursor(kind="snapshot", snapshot_revision=5, after_link_key="ilk1:private")
    )
    fixed_response = client.get("/identity-links/snapshot", params={"cursor": fixed_cursor})
    assert fixed_response.status_code == 200
    assert fixed_response.json()["snapshot_revision"] == 5
    assert repo.snapshot_calls == [(5, "ilk1:private", 50)]


def test_oauth_snapshot_cursor_rechecks_readiness() -> None:
    app = build_oauth2_app()
    app.dependency_overrides[get_current_user_or_oauth_client] = _oauth_reader
    app.dependency_overrides[get_identity_link_revision_repo] = lambda: _AuthorizedRepo(
        [], 8, ready=False
    )
    cursor = encode_identity_link_cursor(
        IdentityLinkCursor(kind="snapshot", snapshot_revision=5, after_link_key="ilk1:private")
    )
    response = TestClient(app).get("/identity-links/snapshot", params={"cursor": cursor})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_link_snapshot_not_ready"
