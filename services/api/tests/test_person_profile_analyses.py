"""Authenticated Person profile-analysis API contracts."""

from __future__ import annotations

import pytest
from src.graph.mappers_profile_analysis import (
    map_person_profile_analyses,
    map_profile_analysis_history_item,
)
from src.graph.queries.profile_analysis import (
    GET_PERSON_PROFILE_ANALYSES,
    GET_PERSON_PROFILE_ANALYSIS_HISTORY,
)
from src.types_profile_analysis import ProfileAnalysisType


def _attempt(
    analysis_type: ProfileAnalysisType,
    *,
    revision: int = 7,
    analysis_id: str | None = None,
    content: str = "Supported analysis [order-1]",
) -> dict[str, object]:
    return {
        "analysis_id": analysis_id or f"analysis-{analysis_type}",
        "person_id": "canonical-person",
        "analysis_type": analysis_type,
        "status": "succeeded",
        "content": content,
        "input_revision": revision,
        "input_fingerprint": "sha256-fingerprint",
        "prompt_version": f"{analysis_type}-profile-v1",
        "provider": "proclaude",
        "model": "analysis-model",
        "started_at": "2099-07-21T01:00:00+00:00",
        "completed_at": "2099-07-21T01:02:00+00:00",
        "attempt_number": 2,
    }


def _current_record(
    *,
    revision: int = 7,
    sales: dict[str, object] | None = None,
    contact: dict[str, object] | None = None,
    claim_active: bool = False,
    sales_claim_active: bool | None = None,
    contact_claim_active: bool | None = None,
    sales_failure: dict[str, object] | None = None,
    contact_failure: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "input_revision": revision,
        "claim_active": claim_active,
        "sales_claim_active": (claim_active if sales_claim_active is None else sales_claim_active),
        "contact_claim_active": (
            claim_active if contact_claim_active is None else contact_claim_active
        ),
        "sales_currents": [] if sales is None else [sales],
        "contact_currents": [] if contact is None else [contact],
        "sales_failure": sales_failure,
        "contact_failure": contact_failure,
        "sales_request_queued": False,
        "contact_request_queued": False,
        "sales_force_attempts_remaining": 3,
        "contact_force_attempts_remaining": 3,
        "sales_force_available_at": None,
        "contact_force_available_at": None,
    }


@pytest.mark.parametrize(
    ("record", "sales_state", "contact_state", "overall"),
    [
        (
            _current_record(
                sales=_attempt("sales"),
                contact=_attempt("contact_tracing"),
                claim_active=True,
                sales_failure={"failure_code": "provider_unavailable"},
            ),
            "ready",
            "ready",
            "ready",
        ),
        (
            _current_record(
                sales=_attempt("sales", revision=6),
                contact=_attempt("contact_tracing", revision=6),
                claim_active=True,
                sales_failure={"failure_code": "provider_unavailable"},
                contact_failure={"failure_code": "invalid_output"},
            ),
            "running",
            "running",
            "running",
        ),
        (
            _current_record(
                sales_failure={"failure_code": "provider_unavailable"},
            ),
            "failed",
            "idle",
            "failed",
        ),
        (
            _current_record(
                sales=_attempt("sales"),
                contact_failure={"failure_code": "invalid_output"},
            ),
            "ready",
            "failed",
            "partial",
        ),
        (
            _current_record(),
            "idle",
            "idle",
            "pending",
        ),
    ],
)
def test_current_mapper_applies_slot_and_overall_state_precedence(
    record: dict[str, object],
    sales_state: str,
    contact_state: str,
    overall: str,
) -> None:
    mapped = map_person_profile_analyses(record)

    assert mapped.sales.refresh_state == sales_state
    assert mapped.contact_tracing.refresh_state == contact_state
    assert mapped.refresh_state == overall


def test_current_mapper_retains_stale_success_when_refresh_failed() -> None:
    mapped = map_person_profile_analyses(
        _current_record(
            sales=_attempt("sales", revision=6, content="Retained prior sales output"),
            sales_failure={"failure_code": "provider_unavailable"},
        )
    )

    assert mapped.sales.current is not None
    assert mapped.sales.current.content == "Retained prior sales output"
    assert mapped.sales.current.generated_age_display
    assert mapped.sales.stale is True
    assert mapped.sales.refresh_state == "failed"
    assert mapped.sales.failure_code == "provider_unavailable"


def test_live_claim_does_not_hide_permanent_failure_for_other_due_type() -> None:
    mapped = map_person_profile_analyses(
        _current_record(
            claim_active=True,
            sales_claim_active=False,
            contact_claim_active=True,
            sales_failure={
                "failure_code": "invalid_output",
                "retryable": False,
                "next_retry_at": None,
            },
        )
    )

    assert mapped.sales.refresh_state == "failed"
    assert mapped.sales.failure_code == "invalid_output"
    assert mapped.contact_tracing.refresh_state == "running"
    assert mapped.refresh_state == "running"


def test_live_claim_does_not_hide_delayed_retry_failure() -> None:
    mapped = map_person_profile_analyses(
        _current_record(
            claim_active=True,
            sales_claim_active=False,
            contact_claim_active=True,
            sales_failure={
                "failure_code": "rate_limited",
                "retryable": True,
                "next_retry_at": "2026-07-21T02:00:00+00:00",
            },
        )
    )

    assert mapped.sales.refresh_state == "retrying"
    assert mapped.sales.failure_code is None
    assert mapped.contact_tracing.refresh_state == "running"


def test_current_mapper_drops_unsafe_failure_code() -> None:
    mapped = map_person_profile_analyses(
        _current_record(sales_failure={"failure_code": "provider body: secret details"})
    )

    assert mapped.sales.refresh_state == "failed"
    assert mapped.sales.failure_code is None


def test_current_mapper_rejects_non_success_current_pointer() -> None:
    current = _attempt("sales")
    current["status"] = "failed"

    with pytest.raises(ValueError, match="current pointer must reference a success"):
        map_person_profile_analyses(_current_record(sales=current))


def test_current_mapper_rejects_duplicate_pointer_targets() -> None:
    record = _current_record(sales=_attempt("sales"))
    record["sales_currents"] = [
        _attempt("sales", analysis_id="analysis-sales-1"),
        _attempt("sales", analysis_id="analysis-sales-2"),
    ]

    with pytest.raises(ValueError, match="multiple current pointers"):
        map_person_profile_analyses(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("analysis_id", ""),
        ("provider", 123),
        ("content", " "),
        ("started_at", "not-a-timestamp"),
    ],
)
def test_current_mapper_rejects_invalid_required_graph_fields(
    field: str,
    value: object,
) -> None:
    current = _attempt("sales")
    current[field] = value

    with pytest.raises((TypeError, ValueError)):
        map_person_profile_analyses(_current_record(sales=current))


def test_history_mapper_exposes_safe_terminal_metadata() -> None:
    item = map_profile_analysis_history_item(
        {
            "analysis": {
                **_attempt("sales"),
                "status": "failed",
                "content": None,
                "failure_code": "rate_limited",
                "retryable": True,
                "next_retry_at": "2026-07-21T01:10:00+00:00",
            }
        }
    )

    assert item.status == "failed"
    assert item.content is None
    assert item.failure_code == "rate_limited"
    assert item.retryable is True
    assert item.next_retry_at == "2026-07-21T01:10:00+00:00"
    assert item.completed_at_display


def test_queries_resolve_canonical_person_and_project_only_safe_fields() -> None:
    combined = "\n".join(
        (
            GET_PERSON_PROFILE_ANALYSES,
            GET_PERSON_PROFILE_ANALYSIS_HISTORY,
        )
    )

    assert "MATCH (requested:Person {person_id: $person_id})" in combined
    assert "coalesce(canonical, requested) AS person" in combined
    assert "$analysis_type" in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    assert "$skip" in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    assert "$limit" in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    assert "sales_claim_active" in GET_PERSON_PROFILE_ANALYSES
    assert "contact_claim_active" in GET_PERSON_PROFILE_ANALYSES
    assert "sales_request_queued" in GET_PERSON_PROFILE_ANALYSES
    assert "sales_request_running" in GET_PERSON_PROFILE_ANALYSES
    assert "sales_force_attempts_remaining" in GET_PERSON_PROFILE_ANALYSES
    assert "sales_failure {.analysis_id, .failure_code, .retryable, .next_retry_at}" in (
        GET_PERSON_PROFILE_ANALYSES
    )
    assert "sales_currents" in GET_PERSON_PROFILE_ANALYSES
    assert "contact_currents" in GET_PERSON_PROFILE_ANALYSES
    assert "head(collect(sales))" not in GET_PERSON_PROFILE_ANALYSES
    assert "head(collect(contact))" not in GET_PERSON_PROFILE_ANALYSES
    assert "{status: 'succeeded'}" not in GET_PERSON_PROFILE_ANALYSES
    assert "analysis.status IN ['succeeded', 'failed', 'obsolete']" in (
        GET_PERSON_PROFILE_ANALYSIS_HISTORY
    )
    assert "ORDER BY analysis.completed_at DESC, analysis.analysis_id DESC" in (
        GET_PERSON_PROFILE_ANALYSIS_HISTORY
    )
    assert "count(analysis) AS total" in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    assert "collect(analysis" in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    assert "total," in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    assert "analyses" in GET_PERSON_PROFILE_ANALYSIS_HISTORY
    for forbidden in (
        "prompt_text",
        "snapshot",
        "raw_payload",
        "provider_body",
        "preferred_nric",
        "preferred_phone",
        "preferred_email",
    ):
        assert forbidden not in combined


def test_current_mapper_marks_expired_output_invalid_and_requestable() -> None:
    current = _attempt("sales")
    current["completed_at"] = "2026-07-20T01:02:00+00:00"
    mapped = map_person_profile_analyses(_current_record(sales=current))

    assert mapped.sales.expired is True
    assert mapped.sales.valid is False
    assert mapped.sales.auto_request_allowed is True
    assert mapped.sales.current is not None
    assert mapped.sales.current.generated_age_display.endswith("ago")


def test_current_mapper_marks_running_request_only_for_its_type() -> None:
    record = _current_record()
    record["sales_claim_active"] = True
    mapped = map_person_profile_analyses(record)

    assert mapped.sales.refresh_state == "running"
    assert mapped.contact_tracing.refresh_state == "idle"


def test_current_mapper_marks_queued_request_pending_without_blocking_other_type() -> None:
    record = _current_record()
    record["sales_request_queued"] = True
    mapped = map_person_profile_analyses(record)

    assert mapped.sales.refresh_state == "pending"
    assert mapped.sales.auto_request_allowed is False
    assert mapped.contact_tracing.refresh_state == "idle"
