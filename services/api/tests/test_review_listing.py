"""Tests for review-case listing: query builders + route filters/sort/pagination."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from src.auth.deps import require_human_user
from src.auth.models import AuthUser
from src.graph.queries.review import (
    REVIEW_SORT_KEYS,
    build_count_review_cases_query,
    build_list_review_cases_query,
)
from src.repositories.protocols.review import ReviewListFilters
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
        assert "OPTIONAL MATCH" not in query
        # Note: "$q" is a substring of "$queue_state", so assert the real predicate.
        assert "$q IS NULL" not in query
        assert "toLower($q)" not in query
        assert "left.preferred_full_name" not in query
    # But the non-search filters are still present.
    assert "$overdue_sla" in count_q


def test_search_path_includes_person_joins() -> None:
    query = build_list_review_cases_query(None, None, has_q=True)
    assert "OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(left:Person)" in query
    assert "OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(right:Person)" in query
    assert "left.preferred_full_name" in query
    assert "right.preferred_email" in query


def test_count_query_shares_filter_clause() -> None:
    count = build_count_review_cases_query(has_q=True)
    assert "RETURN count(rc) AS total" in count
    # Search + filter conditions must be present so counts match the listing.
    assert "$q IS NULL" in count
    assert "$overdue_sla" in count
    assert "$confidence_gte" in count
    # Count query never paginates.
    assert "SKIP" not in count


def test_filter_clause_covers_all_new_filters() -> None:
    query = build_list_review_cases_query(None, None, has_q=True)
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
        "$overdue_sla",
        "$q IS NULL",
    ):
        assert param in query, f"missing filter param {param}"


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
                "priority_gte": "50",
                "priority_lte": "90",
                "decision": "review",
                "engine_type": "heuristic",
                "confidence_gte": "0.6",
                "confidence_lte": "0.95",
                "overdue_sla": "true",
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
    assert repo.captured["priority_gte"] == 50
    assert repo.captured["priority_lte"] == 90
    assert repo.captured["decision"] == "review"
    assert repo.captured["engine_type"] == "heuristic"
    assert repo.captured["confidence_gte"] == 0.6
    assert repo.captured["confidence_lte"] == 0.95
    assert repo.captured["overdue_sla"] is True
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
