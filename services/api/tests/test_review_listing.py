"""Tests for review-case listing: query builders + route filters/sort/pagination."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import get_current_user_or_oauth_client, require_human_user
from src.auth.models import AuthUser
from src.auth.oauth_client_models import OAuthClientUser
from src.error_handlers import register_error_handlers
from src.graph.queries.review import (
    GET_PERSONS_FOR_REVIEW_MERGE,
    REVIEW_SORT_KEYS,
    build_claimed_review_action_cypher,
    build_count_review_cases_query,
    build_list_review_cases_query,
    build_review_action_cypher,
)
from src.repositories.protocols.review import (
    ActionResult,
    BulkActionOutcome,
    ReviewListFilters,
)
from src.routes.review import router
from src.types import MatchDecisionSummary, ReviewCaseSummary

# ── Query builder unit tests ────────────────────────────────────────────────


def test_sort_keys_whitelist() -> None:
    assert REVIEW_SORT_KEYS == frozenset(
        {"priority", "confidence", "sla_due_at", "created_at", "updated_at", "queue_state"}
    )


def test_list_query_default_sort_and_tiebreakers() -> None:
    query = build_list_review_cases_query(None, None, has_q=False)
    assert "ORDER BY rc.priority ASC, rc.sla_due_at ASC, rc.created_at ASC" in query
    assert "SKIP $skip LIMIT $limit" in query


def test_list_query_named_sort_with_explicit_order() -> None:
    query = build_list_review_cases_query("confidence", "asc", has_q=False)
    assert "ORDER BY md.confidence ASC, rc.sla_due_at ASC, rc.created_at ASC" in query


def test_list_query_named_sort_uses_per_column_default_direction() -> None:
    # confidence defaults to DESC when no explicit order is given.
    query = build_list_review_cases_query("confidence", None, has_q=False)
    assert "ORDER BY md.confidence DESC" in query


def test_list_query_unknown_sort_falls_back_to_priority() -> None:
    query = build_list_review_cases_query("not_a_column", "desc", has_q=False)
    assert "ORDER BY rc.priority DESC" in query


def test_no_search_path_skips_person_joins() -> None:
    # Without q, the query must not reference the person joins or the q predicate.
    list_q = build_list_review_cases_query(None, None, has_q=False)
    count_q = build_count_review_cases_query(has_q=False)
    for query in (list_q, count_q):
        # The search-only person joins (left:Person/right:Person) are absent on
        # the no-search path. Display OPTIONAL MATCHes (left/right without label,
        # for address/source/sales) are always present and checked separately.
        assert "OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)" not in query
        assert "OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)" not in query
        # Note: "$q" is a substring of "$queue_state", so assert the real predicate.
        assert "$q IS NULL" not in query
        assert "toLower($q)" not in query
        assert "left.preferred_full_name" not in query
    # No inactive optional filters are emitted.
    assert "$overdue_sla" not in count_q


def test_search_path_includes_person_joins() -> None:
    query = build_list_review_cases_query(None, None, has_q=True)
    assert "OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)" in query
    assert "OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)" in query
    assert "left.preferred_full_name" in query
    assert "right.preferred_email" in query


def test_person_filter_path_includes_joins_without_search_predicate() -> None:
    # person_id alone pulls in the person joins and its predicate, but not the
    # q search predicate.
    list_q = build_list_review_cases_query(None, None, has_q=False, has_person=True)
    count_q = build_count_review_cases_query(has_q=False, has_person=True)
    for query in (list_q, count_q):
        assert "MATCH (person:Person {person_id: $person_id})" in query
        assert "MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(person)" in query
        assert "WITH DISTINCT md" in query
        assert "OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)" in query
        assert "left.person_id = $person_id" not in query
        assert "right.person_id = $person_id" not in query
        assert "toLower($q)" not in query


def test_no_person_filter_path_skips_person_predicate() -> None:
    query = build_list_review_cases_query(None, None, has_q=False, has_person=False)
    assert "$person_id" not in query


def test_person_filter_combines_with_search() -> None:
    query = build_list_review_cases_query(None, None, has_q=True, has_person=True)
    assert "toLower($q)" in query
    assert "MATCH (person:Person {person_id: $person_id})" in query
    # Joins must appear exactly once even when both flags are set.
    assert query.count("OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)") == 1


def test_count_query_shares_filter_clause() -> None:
    active_filters = frozenset({"overdue_sla", "confidence_gte"})
    count = build_count_review_cases_query(has_q=True, active_filters=active_filters)
    assert "RETURN count(rc) AS total" in count
    # Search + filter conditions must be present so counts match the listing.
    assert "toLower(rc.review_case_id) CONTAINS toLower($q)" in count
    assert "rc.sla_due_at < datetime()" in count
    assert "$confidence_gte" in count
    # Count query never paginates.
    assert "SKIP" not in count


def test_filter_clause_covers_all_new_filters() -> None:
    active_filters = frozenset(
        {
            "queue_state",
            "assigned_to",
            "priority_lte",
            "priority_gte",
            "decision",
            "engine_type",
            "confidence_gte",
            "confidence_lte",
            "created_after",
            "created_before",
            "sla_due_after",
            "sla_due_before",
            "overdue_sla",
            "resolved",
        }
    )
    query = build_list_review_cases_query(
        None,
        None,
        has_q=True,
        active_filters=active_filters,
    )
    for param in (
        "$queue_state",
        "$assigned_to",
        "$priority_lte",
        "$priority_gte",
        "$decision",
        "$engine_type",
        "$confidence_gte",
        "$confidence_lte",
        "$created_after",
        "$created_before",
        "$sla_due_after",
        "$sla_due_before",
        "$resolved",
    ):
        assert param in query, f"missing filter param {param}"
    assert "rc.sla_due_at < datetime()" in query
    assert "toLower(rc.review_case_id) CONTAINS toLower($q)" in query


def test_resolved_filter_is_emitted_without_person_joins() -> None:
    # The resolved filter only touches rc.queue_state, so it must be present
    # even on the no-search path (which skips the person joins).
    query = build_list_review_cases_query(
        None,
        None,
        has_q=False,
        has_person=False,
        active_filters=frozenset({"resolved"}),
    )
    closed_states = "['resolved', 'cancelled', 'expired', 'dismissed', 'superseded_by_policy']"
    assert f"(rc.queue_state IN {closed_states}) = $resolved" in query
    # No-search path skips the search-only person joins (left:Person/right:Person);
    # display OPTIONAL MATCHes are always present.
    assert "OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)" not in query
    assert "OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)" not in query


def test_inactive_filters_are_not_emitted() -> None:
    query = build_list_review_cases_query(
        None,
        None,
        has_q=False,
        active_filters=frozenset({"queue_state"}),
    )

    assert "$queue_state" in query
    assert "$assigned_to" not in query
    assert "$overdue_sla" not in query


def test_pending_source_bypass_is_scoped_to_terminal_disposition_states() -> None:
    # Only expired/dismissed/superseded_by_policy cases skip the
    # pending-source requirement, so they stay retrievable after an involved
    # SourceRecord changes lifecycle_status.
    terminal_bypass = (
        "(rc.queue_state IN ['expired', 'dismissed', 'superseded_by_policy'] OR NOT EXISTS {"
    )
    for query in (
        build_list_review_cases_query(None, None, has_q=False),
        build_count_review_cases_query(has_q=False),
    ):
        assert terminal_bypass in query
        # The pending-source requirement itself is unchanged.
        assert "involved:SourceRecord" in query
        assert "involved.lifecycle_status <> 'pending_review'" in query


def test_persons_for_review_merge_projects_engine_type_and_source_record_absence() -> None:
    assert "OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)" in GET_PERSONS_FOR_REVIEW_MERGE
    assert "OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)" in GET_PERSONS_FOR_REVIEW_MERGE
    assert "md.engine_type AS engine_type" in GET_PERSONS_FOR_REVIEW_MERGE
    assert "NOT EXISTS { MATCH (md)-[:ABOUT_LEFT|ABOUT_RIGHT]->(:SourceRecord) }" in (
        GET_PERSONS_FOR_REVIEW_MERGE
    )
    assert "AS has_no_source_record" in GET_PERSONS_FOR_REVIEW_MERGE
    # A missing person side must not break the completion projection.
    assert "CASE WHEN left IS NULL THEN 0" in GET_PERSONS_FOR_REVIEW_MERGE
    assert "CASE WHEN right IS NULL THEN 0" in GET_PERSONS_FOR_REVIEW_MERGE


def test_review_action_cypher_defaults_preserve_legacy_set_clauses() -> None:
    query = build_review_action_cypher("merge", None)

    assert "rc.queue_state = $new_state" in query
    assert "rc.resolution = $resolution" in query
    assert "rc.resolved_at = datetime()" in query
    assert "rc.resolved_by" not in query
    assert "rc.resolution_reason_code" not in query
    assert "RETURN rc {.review_case_id, .queue_state, .resolution, .created_at}" in query


def test_review_action_cypher_adds_audit_fields_only_when_enabled() -> None:
    query = build_review_action_cypher(
        "merge",
        None,
        include_audit_fields=True,
        has_reason_code=True,
    )

    assert "rc.resolved_by = $actor_id" in query
    assert "rc.resolution_reason_code = $reason_code" in query


def test_review_action_cypher_omits_audit_fields_without_resolution() -> None:
    query = build_review_action_cypher(
        None,
        None,
        include_audit_fields=True,
        has_reason_code=True,
    )

    assert "rc.resolved_by" not in query
    assert "rc.resolution_reason_code" not in query


def test_claimed_review_action_cypher_forwards_audit_field_options() -> None:
    query = build_claimed_review_action_cypher(
        "merge",
        None,
        include_audit_fields=True,
        has_reason_code=True,
    )

    assert "rc.resolved_by = $actor_id" in query
    assert "rc.resolution_reason_code = $reason_code" in query
    assert "rc.lifecycle_claim_token = $claim_token" in query


# ── Route-level tests ───────────────────────────────────────────────────────


class FakeReviewRepo:
    def __init__(self) -> None:
        self.captured: ReviewListFilters | None = None
        self.skip: int | None = None
        self.limit: int | None = None

    async def get_page(
        self, filters: ReviewListFilters, skip: int, limit: int
    ) -> tuple[list[ReviewCaseSummary], int]:
        self.captured = filters
        self.skip = skip
        self.limit = limit
        summary = ReviewCaseSummary(
            review_case_id="rc-1",
            queue_state="open",
            priority=80,
            match_decision=MatchDecisionSummary(
                match_decision_id="md-1",
                engine_type="heuristic",
                decision="review",
                confidence=0.72,
            ),
        )
        return [summary], 42


async def _review_user() -> AuthUser:
    return AuthUser(
        email="reviewer@example.com",
        google_sub="reviewer-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Reviewer",
    )


def _make_app(repo: FakeReviewRepo) -> FastAPI:
    from src.repositories.deps import get_review_repo

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_review_repo] = lambda: repo
    app.dependency_overrides[require_human_user] = _review_user
    return app


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.mark.anyio
async def test_list_forwards_filters_and_returns_total() -> None:
    repo = FakeReviewRepo()
    app = _make_app(repo)
    async with _client(app) as client:
        response = await client.get(
            "/v1/review-cases",
            params={
                "queue_state": "open",
                "person_id": "p-123",
                "priority_gte": "50",
                "priority_lte": "90",
                "decision": "review",
                "engine_type": "heuristic",
                "confidence_gte": "0.6",
                "confidence_lte": "0.95",
                "overdue_sla": "true",
                "resolved": "false",
                "q": "ana tan",
                "sort_by": "confidence",
                "sort_order": "desc",
                "limit": "25",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["review_case_id"] == "rc-1"
    assert body["meta"]["total_count"] == 42
    assert body["meta"]["next_cursor"] is not None  # 0 + 25 < 42

    assert repo.captured is not None
    assert repo.captured["queue_state"] == "open"
    assert repo.captured["person_id"] == "p-123"
    assert repo.captured["priority_gte"] == 50
    assert repo.captured["priority_lte"] == 90
    assert repo.captured["decision"] == "review"
    assert repo.captured["engine_type"] == "heuristic"
    assert repo.captured["confidence_gte"] == 0.6
    assert repo.captured["confidence_lte"] == 0.95
    assert repo.captured["overdue_sla"] is True
    assert repo.captured["resolved"] is False
    assert repo.captured["q"] == "ana tan"
    assert repo.captured["sort_by"] == "confidence"
    assert repo.captured["sort_order"] == "desc"


@pytest.mark.anyio
async def test_list_short_query_rejected() -> None:
    repo = FakeReviewRepo()
    app = _make_app(repo)
    async with _client(app) as client:
        response = await client.get("/v1/review-cases", params={"q": "ab"})

    assert response.status_code == 400
    assert repo.captured is None  # never reached the repo


@pytest.mark.anyio
async def test_list_invalid_sort_rejected() -> None:
    repo = FakeReviewRepo()
    app = _make_app(repo)
    async with _client(app) as client:
        response = await client.get("/v1/review-cases", params={"sort_by": "bogus"})

    assert response.status_code == 400
    assert repo.captured is None


@pytest.mark.anyio
async def test_list_last_page_has_no_next_cursor() -> None:
    repo = FakeReviewRepo()
    app = _make_app(repo)
    async with _client(app) as client:
        # cursor for skip=40 with limit=25 → 40 + 25 = 65 >= 42 → no next page
        response = await client.get(
            "/v1/review-cases", params={"limit": "25", "cursor": _cursor_for(40)}
        )

    body = response.json()
    assert body["meta"]["total_count"] == 42
    assert body["meta"]["next_cursor"] is None


def _cursor_for(offset: int) -> str:
    import base64

    return base64.b64encode(str(offset).encode("ascii")).decode("ascii")


# ── Review action + bulk action route tests ─────────────────────────────────


class FakeActionRepo:
    """Repository stub that records single-case and bulk action submissions."""

    def __init__(self) -> None:
        self.action_calls: list[str] = []
        self.bulk_calls: list[list[str]] = []
        self.bulk_arguments: list[tuple[str, str, str | None, str | None]] = []
        self.action_result: ActionResult | None = None
        self.bulk_outcome: BulkActionOutcome = {"items": []}

    async def submit_action(
        self,
        review_case_id: str,
        action_type: str,
        new_state: str,
        resolution: str | None,
        notes: str | None,
        follow_up_at: str | None,
        actor_id: str,
        survivor_person_id: str | None,
        golden_profile_selections: list[object],
        *,
        reason_code: str | None = None,
    ) -> ActionResult | None:
        self.action_calls.append(review_case_id)
        return self.action_result

    async def submit_bulk_action(
        self,
        review_case_ids: list[str],
        action_type: str,
        new_state: str,
        resolution: str | None,
        notes: str | None,
        follow_up_at: str | None,
        actor_id: str,
        reason_code: str,
    ) -> BulkActionOutcome:
        self.bulk_calls.append(list(review_case_ids))
        self.bulk_arguments.append((action_type, new_state, resolution, reason_code))
        return self.bulk_outcome


async def _admin_user() -> AuthUser:
    return AuthUser(
        email="admin@example.com",
        google_sub="admin-sub",
        role="admin",
        entity_key=None,
        display_name="Admin",
    )


async def _employee_user() -> AuthUser:
    return AuthUser(
        email="employee@example.com",
        google_sub="employee-sub",
        role="employee",
        entity_key="fundbox",
        display_name="Employee",
    )


async def _oauth_user() -> OAuthClientUser:
    return OAuthClientUser(
        email="oauth:client-1",
        google_sub="client-1",
        role="employee",
        entity_key=None,
        display_name="client-1",
        client_id="client-1",
        key_scopes=["persons:read"],
    )


def _make_action_app(repo: FakeActionRepo, principal: AuthUser | OAuthClientUser) -> FastAPI:
    from src.repositories.deps import get_review_repo

    app = FastAPI()
    app.include_router(router)
    # The production mounts register the shared error handlers, which render
    # every rejected request in the standard error envelope.
    register_error_handlers(app)
    app.dependency_overrides[get_review_repo] = lambda: repo
    app.dependency_overrides[get_current_user_or_oauth_client] = lambda: principal
    return app


def _bulk_body(review_case_ids: list[str]) -> dict[str, object]:
    return {
        "review_case_ids": review_case_ids,
        "action_type": "resolve",
        "reason_code": "confirmed_match",
    }


@pytest.mark.anyio
async def test_bulk_actions_apply_to_every_requested_case() -> None:
    repo = FakeActionRepo()
    repo.bulk_outcome = {
        "items": [
            {
                "review_case_id": "rc-1",
                "status": "updated",
                "queue_state": "resolved",
                "resolution": "resolve",
                "error_code": None,
            },
            {
                "review_case_id": "rc-2",
                "status": "error",
                "queue_state": None,
                "resolution": None,
                "error_code": "resolve_not_applicable",
            },
        ]
    }
    app = _make_action_app(repo, await _admin_user())

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/bulk-actions", json=_bulk_body(["rc-1", "rc-2"])
        )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["items"][0]["status"] == "updated"
    assert body["data"]["items"][0]["queue_state"] == "resolved"
    assert body["data"]["items"][1]["status"] == "error"
    assert body["data"]["items"][1]["error_code"] == "resolve_not_applicable"
    assert repo.bulk_calls == [["rc-1", "rc-2"]]
    assert repo.bulk_arguments == [("resolve", "resolved", "resolve", "confirmed_match")]


@pytest.mark.anyio
async def test_bulk_actions_authorize_an_admin_without_querying_the_review_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeActionRepo()
    repo.bulk_outcome = {"items": []}
    app = _make_action_app(repo, await _admin_user())

    async def fail_entity_lookup(_review_case_id: str) -> list[str]:
        raise AssertionError("an administrator must not trigger an entity lookup")

    monkeypatch.setattr("src.auth.deps.get_entities_for_review_case", fail_entity_lookup)

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/bulk-actions", json=_bulk_body(["rc-1"])
        )

    assert response.status_code == 200
    assert repo.bulk_calls == [["rc-1"]]


@pytest.mark.anyio
async def test_bulk_actions_authorize_an_employee_for_every_requested_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _employee_user())
    looked_up: list[str] = []

    async def entity_lookup(review_case_id: str) -> list[str]:
        looked_up.append(review_case_id)
        return ["fundbox"]

    monkeypatch.setattr("src.auth.deps.get_entities_for_review_case", entity_lookup)

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/bulk-actions", json=_bulk_body(["rc-1", "rc-2"])
        )

    assert response.status_code == 200
    assert looked_up == ["rc-1", "rc-2"]
    assert repo.bulk_calls == [["rc-1", "rc-2"]]


@pytest.mark.anyio
async def test_bulk_actions_reject_a_mixed_entity_batch_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _employee_user())

    async def entity_lookup(review_case_id: str) -> list[str]:
        if review_case_id == "rc-allowed":
            return ["fundbox"]
        return ["other-entity"]

    monkeypatch.setattr("src.auth.deps.get_entities_for_review_case", entity_lookup)

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/bulk-actions", json=_bulk_body(["rc-allowed", "rc-foreign"])
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_entity_scope"
    # The whole batch is rejected before a single case is written.
    assert repo.bulk_calls == []


@pytest.mark.anyio
async def test_bulk_actions_reject_machine_oauth_clients() -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _oauth_user())

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/bulk-actions", json=_bulk_body(["rc-1"])
        )

    assert response.status_code == 403
    assert repo.bulk_calls == []


@pytest.mark.anyio
async def test_bulk_actions_reject_an_empty_batch() -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _admin_user())

    async with _client(app) as client:
        response = await client.post("/v1/review-cases/bulk-actions", json=_bulk_body([]))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert repo.bulk_calls == []


@pytest.mark.anyio
async def test_bulk_actions_reject_a_batch_above_the_size_limit() -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _admin_user())
    case_ids = [f"rc-{index}" for index in range(101)]

    async with _client(app) as client:
        response = await client.post("/v1/review-cases/bulk-actions", json=_bulk_body(case_ids))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert repo.bulk_calls == []


@pytest.mark.anyio
async def test_bulk_actions_accept_the_maximum_batch_size() -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _admin_user())
    case_ids = [f"rc-{index}" for index in range(100)]

    async with _client(app) as client:
        response = await client.post("/v1/review-cases/bulk-actions", json=_bulk_body(case_ids))

    assert response.status_code == 200
    assert repo.bulk_calls == [case_ids]


@pytest.mark.anyio
async def test_bulk_actions_reject_an_unknown_action_type() -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _admin_user())
    body = _bulk_body(["rc-1"])
    body["action_type"] = "merge"

    async with _client(app) as client:
        response = await client.post("/v1/review-cases/bulk-actions", json=body)

    # The shared error handler normalises a body validation failure to 400.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert repo.bulk_calls == []


@pytest.mark.anyio
async def test_bulk_actions_require_a_reason_code() -> None:
    repo = FakeActionRepo()
    app = _make_action_app(repo, await _admin_user())
    body = _bulk_body(["rc-1"])
    del body["reason_code"]

    async with _client(app) as client:
        response = await client.post("/v1/review-cases/bulk-actions", json=body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert repo.bulk_calls == []


@pytest.mark.anyio
async def test_review_action_rejects_a_pair_audit_merge() -> None:
    repo = FakeActionRepo()
    repo.action_result = ActionResult(pair_audit_merge_disabled=True)
    app = _make_action_app(repo, await _admin_user())

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/rc-1/actions", json={"action_type": "merge"}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "pair_audit_merge_disabled"
    assert repo.action_calls == ["rc-1"]


@pytest.mark.anyio
async def test_review_action_rejects_a_resolve_that_is_not_applicable() -> None:
    repo = FakeActionRepo()
    repo.action_result = ActionResult(resolve_not_applicable=True)
    app = _make_action_app(repo, await _admin_user())

    async with _client(app) as client:
        response = await client.post(
            "/v1/review-cases/rc-1/actions", json={"action_type": "resolve"}
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "resolve_not_applicable"
    assert repo.action_calls == ["rc-1"]
