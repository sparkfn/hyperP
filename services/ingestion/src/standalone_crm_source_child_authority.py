"""Strict durable publication parsing and authority reconstruction for source children."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from src.standalone_crm_census_models import (
    CompanySourceChildEnvelope,
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
    LeadSourceChildEnvelope,
    SourceSyncCensusRequest,
    StandaloneCrmCheckpoint,
    StandaloneCrmChildEnvelope,
    StandaloneCrmSourceAvailability,
    StandaloneCrmSourceChildBudgetAuthorization,
    StandaloneCrmSourceChildEnvelope,
    StandaloneCrmSourceChildScope,
    StandaloneCrmSourceChildUnitAuthority,
)
from src.standalone_crm_census_request_parser import parse_stored_census_request
from src.standalone_crm_census_types import StandaloneCrmStreamKind

SOURCE_CHILD_TASK_NAME = "src.standalone_crm_census_tasks.run_standalone_crm_census_unit"
_SOURCE_CHILD_FIELDS = frozenset(
    {
        "census_id",
        "generation",
        "stream_kind",
        "frozen_upper_id",
        "revision_id",
        "task_name",
        "task_id",
        "queue",
        "payload_version",
    }
)


@dataclass(frozen=True)
class StandaloneCrmSourceChildClaim:
    """Durable authority reconstructed by the atomic published-child claim."""

    envelope: StandaloneCrmSourceChildEnvelope
    checkpoint: StandaloneCrmCheckpoint
    request: SourceSyncCensusRequest


def parse_publication_payload(
    raw: Mapping[str, object],
) -> tuple[str, StandaloneCrmChildEnvelope]:
    """Reject raw work before it can reach a source client or durable claim."""
    if set(raw) != _SOURCE_CHILD_FIELDS:
        raise ValueError("standalone CRM child payload must be the exact stored v1 publication")
    if raw.get("revision_id") is not None:
        raise ValueError(
            "mapping child publications cannot execute through the source-child runtime"
        )
    values = _required_child_values(raw)
    envelope = StandaloneCrmChildEnvelope(
        values.census_id,
        values.generation,
        values.stream_kind,
        values.frozen_upper_id,
        None,
        values.task_name,
        values.task_id,
        values.queue,
        values.payload_version,
    )
    if envelope.frozen_upper_id is None:
        raise ValueError("source child requires a positive frozen bound")
    if envelope.task_name != SOURCE_CHILD_TASK_NAME or envelope.frozen_upper_id < 1:
        raise ValueError("source child requires the registered task and a positive frozen bound")
    payload_json = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return payload_json, envelope


@dataclass(frozen=True)
class _ChildValues:
    census_id: str
    generation: int
    stream_kind: StandaloneCrmStreamKind
    frozen_upper_id: int
    task_name: str
    task_id: str
    queue: str
    payload_version: str


def _required_child_values(raw: Mapping[str, object]) -> _ChildValues:
    census_id = raw.get("census_id")
    task_name = raw.get("task_name")
    task_id = raw.get("task_id")
    queue = raw.get("queue")
    payload_version = raw.get("payload_version")
    if (
        not isinstance(census_id, str)
        or not census_id.strip()
        or not isinstance(task_name, str)
        or not task_name.strip()
        or not isinstance(task_id, str)
        or not task_id.strip()
        or not isinstance(queue, str)
        or not queue.strip()
        or not isinstance(payload_version, str)
        or not payload_version.strip()
    ):
        raise ValueError("standalone CRM child payload has missing task identity")
    generation = raw.get("generation")
    bound = raw.get("frozen_upper_id")
    stream_kind = raw.get("stream_kind")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or isinstance(bound, bool)
        or not isinstance(bound, int)
        or bound < 1
        or stream_kind not in {"contact", "lead", "company"}
    ):
        raise ValueError("standalone CRM child payload has invalid bound or stream")
    if stream_kind == "contact":
        kind: StandaloneCrmStreamKind = "contact"
    elif stream_kind == "lead":
        kind = "lead"
    else:
        kind = "company"
    return _ChildValues(
        census_id, generation, kind, bound, task_name, task_id, queue, payload_version
    )


def build_claim(
    published: StandaloneCrmChildEnvelope,
    row: Mapping[str, object],
) -> StandaloneCrmSourceChildClaim:
    """Build only complete typed source authority from a guarded repository row."""
    request_json = _required_str(row, "request_json")
    try:
        decoded = json.loads(request_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("published source child has malformed immutable census request") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("published source child has malformed immutable census request")
    request = parse_stored_census_request(decoded)
    if not isinstance(request, SourceSyncCensusRequest):
        raise RuntimeError("published source child has no immutable source census")
    fence = _positive_int(row, "fence_token")
    owner = _required_str(row, "fence_owner_id")
    cursor = _non_negative_int(row, "last_committed_id")
    processed = _non_negative_int(row, "processed_rows")
    skipped = _non_negative_int(row, "skipped_rows")
    binding_subject = _optional_int(row, "binding_subject_id")
    binding_offset = _optional_int(row, "binding_offset")
    deadline = _required_datetime(row, "attempt_deadline")
    available_at = _required_datetime(row, "available_at")
    bound = published.frozen_upper_id
    if bound is None:
        raise RuntimeError("published source child has no frozen bound")
    if cursor > bound:
        raise RuntimeError("published source child checkpoint exceeds its frozen bound")
    budget = _budget(request, published, fence, owner, deadline)
    unit = StandaloneCrmSourceChildUnitAuthority(
        published.census_id,
        published.stream_kind,
        published.generation,
        fence,
        owner,
        published.task_name,
        published.task_id,
        published.payload_digest(),
    )
    scope = StandaloneCrmSourceChildScope(
        request.source_key, request.source_instance_id, request.control_instance_id
    )
    checkpoint = StandaloneCrmCheckpoint(
        published.census_id,
        published.stream_kind,
        published.frozen_upper_id,
        None,
        cursor,
        binding_subject,
        binding_offset,
        processed,
        skipped,
        published.generation,
        fence,
    )
    availability = StandaloneCrmSourceAvailability(available_at)
    if published.stream_kind == "contact":
        position = None
        if binding_subject is not None and binding_offset is not None:
            position = ContactBindingSubposition(binding_subject, binding_offset)
        envelope: StandaloneCrmSourceChildEnvelope = ContactSourceChildEnvelope(
            scope, unit, bound, cursor, availability, budget, position
        )
    elif published.stream_kind == "lead":
        envelope = LeadSourceChildEnvelope(scope, unit, bound, cursor, availability, budget)
    else:
        envelope = CompanySourceChildEnvelope(scope, unit, bound, cursor, availability, budget)
    return StandaloneCrmSourceChildClaim(envelope, checkpoint, request)


def _budget(
    request: SourceSyncCensusRequest,
    published: StandaloneCrmChildEnvelope,
    fence: int,
    owner: str,
    deadline: str,
) -> StandaloneCrmSourceChildBudgetAuthorization:
    material = ":".join(
        (
            published.census_id,
            str(published.generation),
            published.stream_kind,
            published.task_id,
            str(fence),
        )
    )
    digest = "sha256:" + hashlib.sha256(material.encode("utf-8")).hexdigest()
    return StandaloneCrmSourceChildBudgetAuthorization(
        uuid.uuid5(uuid.NAMESPACE_URL, material).hex,
        digest,
        published.census_id,
        published.stream_kind,
        published.generation,
        fence,
        owner,
        published.task_name,
        published.task_id,
        published.payload_digest(),
        request.budget.max_calls_per_attempt,
        request.budget.max_rows_per_attempt,
        request.budget.max_calls_per_occurrence,
        request.budget.max_rows_per_occurrence,
        deadline,
        request.budget.occurrence_deadline,
    )


def _positive_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RuntimeError(f"published source child has invalid {key}")
    return value


def _non_negative_int(row: Mapping[str, object], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"published source child has invalid {key}")
    return value


def _optional_int(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None:
        return None
    return _positive_int(row, key) if key == "binding_subject_id" else _non_negative_int(row, key)


def _required_str(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"published source child has invalid {key}")
    return value


@runtime_checkable
class _Neo4jDateTime(Protocol):
    def to_native(self) -> datetime: ...


def _required_datetime(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if isinstance(value, _Neo4jDateTime):
        native = value.to_native()
        return native.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value
    raise RuntimeError(f"published source child has invalid {key}")
