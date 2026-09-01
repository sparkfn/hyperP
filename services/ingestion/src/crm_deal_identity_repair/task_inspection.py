"""Bounded, authenticated worker and Redis/Celery task-absence evidence."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair.digests import object_digest
from src.models import JsonValue

TASK_ABSENCE_HMAC_DOMAIN = b"crm-deal-identity-repair-task-absence-v1\x00"
_AFFECTED_TASKS = frozenset({"src.tasks.run_ingestion_task"})


class WorkerInspector(Protocol):
    def inspect(
        self, timeout_seconds: int
    ) -> Mapping[str, Mapping[str, tuple[dict[str, JsonValue], ...]]]: ...


class BrokerInspector(Protocol):
    def inspect(
        self, selectors: tuple[str, ...]
    ) -> Mapping[str, tuple[dict[str, JsonValue], ...]]: ...


class _RedisInventory(Protocol):
    def lrange(self, name: str, start: int, end: int) -> list[object]: ...

    def hgetall(self, name: str) -> dict[object, object]: ...


@dataclass(frozen=True)
class TaskAbsenceEvidence:
    """Complete signed evidence. A non-absent or unknown observation cannot seal."""

    run_id: str
    boundary_digest: str
    owner_id: str
    token: str
    dispatch_revision: int
    topology_digest: str
    selectors: tuple[str, ...]
    expected_workers: tuple[str, ...]
    responding_workers: tuple[str, ...]
    observations: dict[str, tuple[dict[str, JsonValue], ...]]
    broker_observations: dict[str, tuple[dict[str, JsonValue], ...]]
    inspected_at: str
    expires_at: str
    key_id: str
    payload_digest: str
    hmac_hex: str

    def is_fresh_for(
        self,
        *,
        run_id: str,
        boundary_digest: str,
        owner_id: str,
        token: str,
        revision: int,
        now: datetime,
    ) -> bool:
        if (
            self.run_id,
            self.boundary_digest,
            self.owner_id,
            self.token,
            self.dispatch_revision,
        ) != (run_id, boundary_digest, owner_id, token, revision):
            return False
        try:
            return datetime.fromisoformat(self.expires_at) > now
        except ValueError:
            return False

    def payload(self) -> dict[str, JsonValue]:
        return {
            "run_id": self.run_id,
            "boundary_digest": self.boundary_digest,
            "owner_id": self.owner_id,
            "token": self.token,
            "dispatch_revision": self.dispatch_revision,
            "topology_digest": self.topology_digest,
            "selectors": list(self.selectors),
            "expected_workers": list(self.expected_workers),
            "responding_workers": list(self.responding_workers),
            "observations": _json_observations(self.observations),
            "broker_observations": _json_observations(self.broker_observations),
            "inspected_at": self.inspected_at,
            "expires_at": self.expires_at,
            "key_id": self.key_id,
        }


def _json_observations(
    observations: Mapping[str, tuple[dict[str, JsonValue], ...]],
) -> dict[str, JsonValue]:
    return {name: list(tasks) for name, tasks in observations.items()}


def collect_absence_evidence(
    *,
    worker: WorkerInspector,
    broker: BrokerInspector,
    run_id: str,
    control_instance_id: str,
    boundary_digest: str,
    owner_id: str,
    token: str,
    dispatch_revision: int,
    topology_digest: str,
    expected_workers: tuple[str, ...],
    timeout_seconds: int,
    max_age_seconds: int,
    key_id: str,
    secret: bytes,
    now: datetime | None = None,
) -> TaskAbsenceEvidence:
    """Call injected inspectors directly and seal only complete empty observations."""
    if not expected_workers or tuple(sorted(set(expected_workers))) != expected_workers:
        raise RuntimeError("repair expected worker IDs must be a non-empty canonical set")
    if timeout_seconds < 1 or timeout_seconds > 60 or max_age_seconds < 1 or max_age_seconds > 300:
        raise ValueError("repair task inspection bounds are invalid")
    instant = now or datetime.now(UTC)
    selectors = _selectors(run_id, control_instance_id)
    observations = _canonical_observations(worker.inspect(timeout_seconds))
    broker_observations = _canonical_broker_observations(broker.inspect(selectors))
    responders = tuple(sorted(observations))
    if responders != expected_workers:
        raise RuntimeError("repair expected workers did not all respond")
    if _has_affected_task(observations, selectors) or _has_affected_task(
        broker_observations, selectors
    ):
        raise RuntimeError("repair task or broker delivery remains present")
    inspected_at = instant.isoformat()
    expires_at = (instant + timedelta(seconds=max_age_seconds)).isoformat()
    payload: dict[str, JsonValue] = {
        "run_id": run_id,
        "boundary_digest": boundary_digest,
        "owner_id": owner_id,
        "token": token,
        "dispatch_revision": dispatch_revision,
        "topology_digest": topology_digest,
        "selectors": list(selectors),
        "expected_workers": list(expected_workers),
        "responding_workers": list(responders),
        "observations": _json_observations(observations),
        "broker_observations": _json_observations(broker_observations),
        "inspected_at": inspected_at,
        "expires_at": expires_at,
        "key_id": key_id,
    }
    digest = object_digest(TASK_ABSENCE_HMAC_DOMAIN, payload)
    signature = hmac.new(
        secret, TASK_ABSENCE_HMAC_DOMAIN + canonical_json_bytes(payload), hashlib.sha256
    )
    return TaskAbsenceEvidence(
        run_id,
        boundary_digest,
        owner_id,
        token,
        dispatch_revision,
        topology_digest,
        selectors,
        expected_workers,
        responders,
        observations,
        broker_observations,
        inspected_at,
        expires_at,
        key_id,
        digest,
        signature.hexdigest(),
    )


def verify_absence_evidence(evidence: TaskAbsenceEvidence, *, secret: bytes, now: datetime) -> bool:
    """Recompute signature/digest and reject stale, non-empty, or incomplete proof."""
    payload = evidence.payload()
    expected_digest = object_digest(TASK_ABSENCE_HMAC_DOMAIN, payload)
    expected_hmac = hmac.new(
        secret, TASK_ABSENCE_HMAC_DOMAIN + canonical_json_bytes(payload), hashlib.sha256
    )
    if not hmac.compare_digest(evidence.payload_digest, expected_digest):
        return False
    if not hmac.compare_digest(evidence.hmac_hex, expected_hmac.hexdigest()):
        return False
    if not evidence.expected_workers or evidence.responding_workers != evidence.expected_workers:
        return False
    if not evidence.is_fresh_for(
        run_id=evidence.run_id,
        boundary_digest=evidence.boundary_digest,
        owner_id=evidence.owner_id,
        token=evidence.token,
        revision=evidence.dispatch_revision,
        now=now,
    ):
        return False
    return not _has_affected_task(
        evidence.observations, evidence.selectors
    ) and not _has_affected_task(evidence.broker_observations, evidence.selectors)


class CeleryWorkerInspector:
    """Production adapter; only invoked by the explicitly gated control command."""

    def inspect(
        self, timeout_seconds: int
    ) -> Mapping[str, Mapping[str, tuple[dict[str, JsonValue], ...]]]:
        from src.celery_app import celery_app

        inspector = celery_app.control.inspect(timeout=timeout_seconds)
        result: dict[str, Mapping[str, tuple[dict[str, JsonValue], ...]]] = {}
        for kind, response in (
            ("active", inspector.active()),
            ("reserved", inspector.reserved()),
            ("scheduled", inspector.scheduled()),
        ):
            if response is None:
                raise RuntimeError(f"Celery {kind} inspection did not respond")
            for node, tasks in response.items():
                if not isinstance(node, str) or not isinstance(tasks, list):
                    raise RuntimeError("Celery inspection response is malformed")
                bucket = dict(result.get(node, {}))
                bucket[kind] = tuple(_task_json(item) for item in tasks)
                result[node] = bucket
        return result


class RedisCeleryBrokerInspector:
    """Redis ready/unacked inventory adapter; unknown transport layouts fail closed."""

    def __init__(self, broker_url: str) -> None:
        self._broker_url = broker_url

    def inspect(self, selectors: tuple[str, ...]) -> Mapping[str, tuple[dict[str, JsonValue], ...]]:
        if not self._broker_url.startswith("redis://"):
            raise RuntimeError("unsupported broker topology for repair absence inspection")
        import redis

        client = redis.Redis.from_url(self._broker_url, decode_responses=True)
        ready = _redis_list_inventory(client, "ingestion")
        unacked = _redis_hash_inventory(client, "unacked")
        return {"ready": ready, "unacked": unacked}


def _selectors(run_id: str, control_instance_id: str) -> tuple[str, ...]:
    """Return the exact run/control identities which a delivery must bind.

    Celery's transport UUID is not a business selector.  Only task kwargs (or
    a repair-specific header) identify the operation protected by this fence.
    """
    if not run_id or not control_instance_id:
        raise ValueError("repair task selectors must be non-empty")
    return (f"control_instance_id={control_instance_id}", f"run_id={run_id}")


def _canonical_observations(
    raw: Mapping[str, Mapping[str, tuple[dict[str, JsonValue], ...]]],
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    result: dict[str, tuple[dict[str, JsonValue], ...]] = {}
    for node in sorted(raw):
        per_node = raw[node]
        if set(per_node) != {"active", "reserved", "scheduled"}:
            raise RuntimeError("Celery inspection response has unknown task inventory")
        tasks = tuple(
            task for category in ("active", "reserved", "scheduled") for task in per_node[category]
        )
        result[node] = tuple(sorted(tasks, key=lambda item: canonical_json_bytes(item)))
    return result


def _canonical_broker_observations(
    raw: Mapping[str, tuple[dict[str, JsonValue], ...]],
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    if set(raw) != {"ready", "unacked"}:
        raise RuntimeError("broker inspection topology is unknown")
    return {
        kind: tuple(sorted(raw[kind], key=lambda item: canonical_json_bytes(item))) for kind in raw
    }


def _has_affected_task(
    observations: Mapping[str, tuple[dict[str, JsonValue], ...]], selectors: tuple[str, ...]
) -> bool:
    for tasks in observations.values():
        for task in tasks:
            name = task.get("name")
            if not isinstance(name, str):
                raise RuntimeError("task identity is unknown")
            if name not in _AFFECTED_TASKS:
                continue
            control_instance_id, run_id = _task_selectors(task)
            if control_instance_id is None or run_id is None:
                raise RuntimeError("task selector identity is unknown")
            if (
                f"control_instance_id={control_instance_id}" in selectors
                and f"run_id={run_id}" in selectors
            ):
                return True
    return False


def _task_json(value: object) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise RuntimeError("Celery task observation is malformed")
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError("Celery task observation has a malformed key")
        if key in {"kwargs", "headers"}:
            result[key] = _task_json_object(item, key)
        elif item is None or isinstance(item, (str, int, bool, float)):
            result[key] = item
    if "name" not in result:
        raise RuntimeError("Celery task observation has no name")
    if not isinstance(result["name"], str):
        raise RuntimeError("Celery task observation name is malformed")
    return result


def _task_json_object(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise RuntimeError(f"Celery task {label} are malformed")
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RuntimeError(f"Celery task {label} keys are malformed")
        result[key] = _task_json_value(item, label)
    return result


def _task_json_value(value: object, label: str) -> JsonValue:
    if value is None or isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, dict):
        return _task_json_object(value, label)
    if isinstance(value, list):
        return [_task_json_value(item, label) for item in value]
    raise RuntimeError(f"Celery task {label} value is malformed")


def _redis_list_inventory(client: object, key: str) -> tuple[dict[str, JsonValue], ...]:
    values = cast(_RedisInventory, client).lrange(key, 0, -1)
    return tuple(_decode_broker_delivery(value) for value in values)


def _redis_hash_inventory(client: object, key: str) -> tuple[dict[str, JsonValue], ...]:
    values = cast(_RedisInventory, client).hgetall(key)
    return tuple(_decode_broker_delivery(value) for value in values.values())


def _task_selectors(task: Mapping[str, JsonValue]) -> tuple[str | None, str | None]:
    """Extract exact repair selectors from inspected Celery task metadata."""
    values: list[Mapping[str, JsonValue]] = [task]
    kwargs = task.get("kwargs")
    headers = task.get("headers")
    if kwargs is not None:
        if not isinstance(kwargs, dict):
            raise RuntimeError("task kwargs are malformed")
        values.append(kwargs)
    if headers is not None:
        if not isinstance(headers, dict):
            raise RuntimeError("task headers are malformed")
        values.append(headers)
    control: str | None = None
    run: str | None = None
    for value in values:
        candidate_control = value.get("control_instance_id")
        candidate_run = value.get("repair_run_id", value.get("bitrix_generation_id"))
        if isinstance(candidate_control, str):
            if control is not None and control != candidate_control:
                raise RuntimeError("task control selector is ambiguous")
            control = candidate_control
        if isinstance(candidate_run, str):
            if run is not None and run != candidate_run:
                raise RuntimeError("task run selector is ambiguous")
            run = candidate_run
    return control, run


def _decode_broker_delivery(value: object) -> dict[str, JsonValue]:
    """Decode the supported Celery JSON envelope; unknown transport is unsafe."""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise RuntimeError("broker delivery is malformed")
    try:
        envelope = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("broker delivery is not JSON") from exc
    if not isinstance(envelope, dict):
        raise RuntimeError("broker delivery envelope is malformed")
    headers = envelope.get("headers")
    if not isinstance(headers, dict):
        raise RuntimeError("broker delivery headers are malformed")
    name = headers.get("task")
    if not isinstance(name, str):
        raise RuntimeError("broker delivery task name is unknown")
    body = envelope.get("body")
    kwargs = _decode_celery_kwargs(body)
    result: dict[str, JsonValue] = {"name": name, "headers": headers, "kwargs": kwargs}
    return result


def _decode_celery_kwargs(body: object) -> dict[str, JsonValue]:
    if not isinstance(body, str):
        raise RuntimeError("broker delivery body is malformed")
    try:
        decoded = base64.b64decode(body, validate=True)
        payload = json.loads(decoded)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("broker delivery body is not a Celery JSON payload") from exc
    if not isinstance(payload, list) or len(payload) != 3 or not isinstance(payload[1], dict):
        raise RuntimeError("broker delivery task payload is malformed")
    kwargs = payload[1]
    if not all(isinstance(key, str) for key in kwargs):
        raise RuntimeError("broker delivery kwargs are malformed")
    return cast(dict[str, JsonValue], kwargs)
