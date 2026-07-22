"""Map safe Neo4j projections into Person profile-analysis API models."""

from __future__ import annotations

import re

from src.display_format import format_display_datetime
from src.graph.converters import (
    GraphRecord,
    GraphValue,
    to_datetime,
    to_iso_or_empty,
)
from src.types_profile_analysis import (
    PersonProfileAnalyses,
    ProfileAnalysisCurrent,
    ProfileAnalysisHistoryItem,
    ProfileAnalysisRefreshState,
    ProfileAnalysisSlot,
    ProfileAnalysisSlotRefreshState,
    ProfileAnalysisStatus,
    ProfileAnalysisType,
)

_SAFE_FAILURE_CODE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


def _as_record(value: GraphValue) -> GraphRecord:
    if isinstance(value, dict):
        return value
    return {}


def _required_record(value: GraphValue, label: str) -> GraphRecord:
    if not isinstance(value, dict):
        raise TypeError(f"profile analysis {label} must be a map")
    return value


def _required_str(record: GraphRecord, key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str):
        raise TypeError(f"profile analysis {key} must be a string")
    if not value.strip():
        raise ValueError(f"profile analysis {key} must not be empty")
    return value


def _optional_str(record: GraphRecord, key: str) -> str | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"profile analysis {key} must be a string or null")
    if not value.strip():
        raise ValueError(f"profile analysis {key} must not be empty")
    return value


def _required_int(record: GraphRecord, key: str) -> int:
    value = record.get(key)
    if type(value) is not int:
        raise TypeError(f"profile analysis {key} must be an integer")
    return value


def _required_bool(record: GraphRecord, key: str) -> bool:
    value = record.get(key)
    if type(value) is not bool:
        raise TypeError(f"profile analysis {key} must be a boolean")
    return value


def _optional_bool(record: GraphRecord, key: str) -> bool | None:
    value = record.get(key)
    if value is None:
        return None
    if type(value) is not bool:
        raise TypeError(f"profile analysis {key} must be a boolean or null")
    return value


def _required_timestamp(record: GraphRecord, key: str) -> str:
    value = record.get(key)
    parsed = to_datetime(value)
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"profile analysis {key} must be a timezone-aware timestamp")
    return to_iso_or_empty(value)


def _optional_timestamp(record: GraphRecord, key: str) -> str | None:
    if record.get(key) is None:
        return None
    return _required_timestamp(record, key)


def _analysis_type(value: GraphValue) -> ProfileAnalysisType:
    if value == "sales":
        return "sales"
    if value == "contact_tracing":
        return "contact_tracing"
    raise ValueError(f"unexpected profile analysis type: {value!r}")


def _analysis_status(value: GraphValue) -> ProfileAnalysisStatus:
    if value == "succeeded":
        return "succeeded"
    if value == "failed":
        return "failed"
    if value == "obsolete":
        return "obsolete"
    raise ValueError(f"unexpected profile analysis status: {value!r}")


def _safe_failure_code(value: GraphValue) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) > 64 or _SAFE_FAILURE_CODE.fullmatch(value) is None:
        return None
    return value


def _map_current(value: GraphValue) -> ProfileAnalysisCurrent:
    raw = _required_record(value, "current pointer target")
    if _analysis_status(raw.get("status")) != "succeeded":
        raise ValueError("profile analysis current pointer must reference a success")
    completed_at = _required_timestamp(raw, "completed_at")
    return ProfileAnalysisCurrent(
        analysis_id=_required_str(raw, "analysis_id"),
        person_id=_required_str(raw, "person_id"),
        analysis_type=_analysis_type(raw.get("analysis_type")),
        status="succeeded",
        content=_required_str(raw, "content"),
        input_revision=_required_int(raw, "input_revision"),
        input_fingerprint=_required_str(raw, "input_fingerprint"),
        prompt_version=_required_str(raw, "prompt_version"),
        provider=_required_str(raw, "provider"),
        model=_required_str(raw, "model"),
        started_at=_required_timestamp(raw, "started_at"),
        completed_at=completed_at,
        completed_at_display=format_display_datetime(completed_at),
        attempt_number=_required_int(raw, "attempt_number"),
    )


def _map_current_targets(value: GraphValue) -> ProfileAnalysisCurrent | None:
    if not isinstance(value, list):
        raise TypeError("profile analysis current pointers must be a list")
    if len(value) > 1:
        raise ValueError("profile analysis has multiple current pointers for one type")
    if not value:
        return None
    return _map_current(value[0])


def _slot_state(
    *,
    fresh: bool,
    claim_active: bool,
    failure_exists: bool,
    retry_scheduled: bool,
) -> ProfileAnalysisSlotRefreshState:
    if fresh:
        return "ready"
    if claim_active:
        return "running"
    if retry_scheduled:
        return "retrying"
    if failure_exists:
        return "failed"
    return "pending"


def _map_slot(
    current_value: GraphValue,
    failure_value: GraphValue,
    *,
    analysis_type: ProfileAnalysisType,
    input_revision: int,
    claim_active: bool,
) -> ProfileAnalysisSlot:
    current = _map_current_targets(current_value)
    if current is not None and current.analysis_type != analysis_type:
        raise ValueError("profile analysis current pointer has the wrong analysis type")
    fresh = current is not None and current.input_revision == input_revision
    failure = _as_record(failure_value)
    retryable = _optional_bool(failure, "retryable") if failure else None
    next_retry_at = _optional_timestamp(failure, "next_retry_at") if failure else None
    if retryable is True and next_retry_at is None:
        raise ValueError("retryable profile analysis failure requires next_retry_at")
    state = _slot_state(
        fresh=fresh,
        claim_active=claim_active,
        failure_exists=bool(failure),
        retry_scheduled=retryable is True,
    )
    return ProfileAnalysisSlot(
        current=current,
        stale=current is not None and not fresh,
        refresh_state=state,
        failure_code=(
            _safe_failure_code(failure.get("failure_code")) if state == "failed" else None
        ),
    )


def _overall_state(
    sales: ProfileAnalysisSlot,
    contact: ProfileAnalysisSlot,
) -> ProfileAnalysisRefreshState:
    states = (sales.refresh_state, contact.refresh_state)
    if "running" in states:
        return "running"
    if "retrying" in states:
        return "retrying"
    ready_count = states.count("ready")
    if ready_count == 2:
        return "ready"
    if ready_count == 1:
        return "partial"
    if "failed" in states:
        return "failed"
    return "pending"


def map_person_profile_analyses(record: GraphRecord) -> PersonProfileAnalyses:
    """Map current slots and apply the documented refresh-state precedence."""
    input_revision = _required_int(record, "input_revision")
    sales = _map_slot(
        record.get("sales_currents"),
        record.get("sales_failure"),
        analysis_type="sales",
        input_revision=input_revision,
        claim_active=_required_bool(record, "sales_claim_active"),
    )
    contact = _map_slot(
        record.get("contact_currents"),
        record.get("contact_failure"),
        analysis_type="contact_tracing",
        input_revision=input_revision,
        claim_active=_required_bool(record, "contact_claim_active"),
    )
    return PersonProfileAnalyses(
        input_revision=input_revision,
        refresh_state=_overall_state(sales, contact),
        sales=sales,
        contact_tracing=contact,
    )


def map_profile_analysis_history_item(record: GraphRecord) -> ProfileAnalysisHistoryItem:
    """Map one terminal history projection without exposing private worker data."""
    raw = _required_record(record.get("analysis"), "history item")
    status = _analysis_status(raw.get("status"))
    content = _optional_str(raw, "content")
    if status == "succeeded" and content is None:
        raise ValueError("succeeded profile analysis history requires content")
    if status == "failed" and content is not None:
        raise ValueError("failed profile analysis history cannot contain content")
    completed_at = _required_timestamp(raw, "completed_at")
    return ProfileAnalysisHistoryItem(
        analysis_id=_required_str(raw, "analysis_id"),
        person_id=_required_str(raw, "person_id"),
        analysis_type=_analysis_type(raw.get("analysis_type")),
        status=status,
        content=content,
        input_revision=_required_int(raw, "input_revision"),
        input_fingerprint=_required_str(raw, "input_fingerprint"),
        prompt_version=_required_str(raw, "prompt_version"),
        provider=_required_str(raw, "provider"),
        model=_required_str(raw, "model"),
        started_at=_required_timestamp(raw, "started_at"),
        completed_at=completed_at,
        completed_at_display=format_display_datetime(completed_at),
        attempt_number=_required_int(raw, "attempt_number"),
        failure_code=_safe_failure_code(raw.get("failure_code")),
        retryable=_optional_bool(raw, "retryable"),
        next_retry_at=_optional_timestamp(raw, "next_retry_at"),
    )
