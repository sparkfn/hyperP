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
from src.crm_deal_identity_repair.control_models import (
    CapturedTaskTopologyIdentity,
    durable_control_token_digest,
)
from src.crm_deal_identity_repair.digests import object_digest
from src.models import JsonValue

TASK_ABSENCE_HMAC_DOMAIN = b"crm-deal-identity-repair-task-absence-v1\x00"
_AFFECTED_TASKS = frozenset({"src.tasks.run_ingestion_task"})


class WorkerInspector(Protocol):
    def inspect(
        self, timeout_seconds: int
    ) -> Mapping[str, Mapping[str, tuple[dict[str, JsonValue], ...]]]: ...


@dataclass(frozen=True)
class BrokerInspection:
    """Canonical Redis/Kombu inventory and the topology used to obtain it."""

    topology: dict[str, JsonValue]
    observations: dict[str, tuple[dict[str, JsonValue], ...]]


class BrokerInspector(Protocol):
    def inspect(self, selectors: tuple[str, ...]) -> BrokerInspection: ...


class _RedisInventory(Protocol):
    def lrange(self, name: str, start: int, end: int) -> list[object]: ...

    def hgetall(self, name: str) -> dict[object, object]: ...

    def zrange(self, name: str, start: int, end: int) -> list[object]: ...


@dataclass(frozen=True)
class TaskAbsenceEvidence:
    """Complete signed evidence. A non-absent or unknown observation cannot seal."""

    run_id: str
    boundary_digest: str
    owner_id: str
    token_digest: str
    dispatch_revision: int
    topology_digest: str
    selectors: tuple[str, ...]
    expected_workers: tuple[str, ...]
    responding_workers: tuple[str, ...]
    observations: dict[str, tuple[dict[str, JsonValue], ...]]
    broker_topology: dict[str, JsonValue]
    broker_observations: dict[str, tuple[dict[str, JsonValue], ...]]
    inspected_at: str
    expires_at: str
    key_id: str
    payload_digest: str
    hmac_hex: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_digest", durable_control_token_digest(self.token_digest))

    def is_fresh_for(
        self,
        *,
        run_id: str,
        boundary_digest: str,
        owner_id: str,
        token_digest: str,
        revision: int,
        now: datetime,
    ) -> bool:
        if (
            self.run_id,
            self.boundary_digest,
            self.owner_id,
            self.token_digest,
            self.dispatch_revision,
        ) != (run_id, boundary_digest, owner_id, token_digest, revision):
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
            "token_digest": self.token_digest,
            "dispatch_revision": self.dispatch_revision,
            "topology_digest": self.topology_digest,
            "selectors": list(self.selectors),
            "expected_workers": list(self.expected_workers),
            "responding_workers": list(self.responding_workers),
            "observations": _json_observations(self.observations),
            "broker_topology": self.broker_topology,
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
    captured_tasks: tuple[CapturedTaskTopologyIdentity, ...],
    boundary_digest: str,
    owner_id: str,
    token_digest: str,
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
    token_digest = durable_control_token_digest(token_digest)
    instant = now or datetime.now(UTC)
    selectors = _selectors(captured_tasks)
    observations = _canonical_observations(worker.inspect(timeout_seconds))
    broker_inventory = broker.inspect(selectors)
    broker_topology = _canonical_broker_topology(broker_inventory.topology)
    broker_observations = _canonical_broker_observations(broker_inventory.observations)
    responders = tuple(sorted(observations))
    if responders != expected_workers:
        raise RuntimeError("repair expected workers did not all respond")
    if _has_affected_task(observations, captured_tasks) or _has_affected_task(
        broker_observations, captured_tasks
    ):
        raise RuntimeError("repair task or broker delivery remains present")
    inspected_at = instant.isoformat()
    expires_at = (instant + timedelta(seconds=max_age_seconds)).isoformat()
    payload: dict[str, JsonValue] = {
        "run_id": run_id,
        "boundary_digest": boundary_digest,
        "owner_id": owner_id,
        "token_digest": token_digest,
        "dispatch_revision": dispatch_revision,
        "topology_digest": topology_digest,
        "selectors": list(selectors),
        "expected_workers": list(expected_workers),
        "responding_workers": list(responders),
        "observations": _json_observations(observations),
        "broker_topology": broker_topology,
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
        token_digest,
        dispatch_revision,
        topology_digest,
        selectors,
        expected_workers,
        responders,
        observations,
        broker_topology,
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
    try:
        canonical_topology = _canonical_broker_topology(evidence.broker_topology)
        canonical_observations = _canonical_broker_observations(evidence.broker_observations)
    except RuntimeError:
        return False
    if (
        evidence.broker_topology != canonical_topology
        or evidence.broker_observations != canonical_observations
    ):
        return False
    if not evidence.is_fresh_for(
        run_id=evidence.run_id,
        boundary_digest=evidence.boundary_digest,
        owner_id=evidence.owner_id,
        token_digest=evidence.token_digest,
        revision=evidence.dispatch_revision,
        now=now,
    ):
        return False
    captured_tasks = _captured_tasks_from_selectors(evidence.selectors)
    return not _has_affected_task(evidence.observations, captured_tasks) and not _has_affected_task(
        canonical_observations, captured_tasks
    )


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

    def inspect(self, selectors: tuple[str, ...]) -> BrokerInspection:
        if not self._broker_url.startswith("redis://"):
            raise RuntimeError("unsupported broker topology for repair absence inspection")
        import redis

        client = redis.Redis.from_url(self._broker_url, decode_responses=True)
        # Kombu's Redis transport stores priority 0 at the bare queue name
        # and nonzero priorities under its configured separator suffix.
        # Inspect every configured priority list; a partial ready inventory is
        # not admissible absence evidence.
        ready = tuple(
            delivery
            for queue_name in _redis_priority_queue_names("ingestion")
            for delivery in _redis_list_inventory(client, queue_name)
        )
        unacked_hash = "unacked"
        unacked_index = "unacked_index"
        unacked = _redis_unacked_inventory(client, unacked_hash, unacked_index)
        topology: dict[str, JsonValue] = {
            "ready_priority_keys": list(_redis_priority_queue_names("ingestion")),
            "unacked_hash": unacked_hash,
            "unacked_index": unacked_index,
            "unacked_wrapper": "kombu-redis-json-v1",
        }
        return BrokerInspection(topology, {"ready": ready, "unacked": unacked})


def _redis_priority_queue_names(queue_name: str) -> tuple[str, ...]:
    """Return Kombu Redis default priority keys for the configured 0..9 topology."""
    return (queue_name, *(f"{queue_name}\x06\x16{priority}" for priority in range(1, 10)))


def _selectors(captured_tasks: tuple[CapturedTaskTopologyIdentity, ...]) -> tuple[str, ...]:
    """Return canonical selectors for the topology captured before the stop.

    A repair ``run_id`` is a control-plane identity, not a Celery identity.
    The task fence instead names each captured generation/logical/attempt tuple.
    """
    if not captured_tasks:
        raise ValueError("captured task topology identities must be non-empty")
    if len(set(captured_tasks)) != len(captured_tasks):
        raise ValueError("captured task topology identities must be unique")
    return tuple(sorted(identity.selector() for identity in captured_tasks))


def _captured_tasks_from_selectors(
    selectors: tuple[str, ...],
) -> tuple[CapturedTaskTopologyIdentity, ...]:
    identities: list[CapturedTaskTopologyIdentity] = []
    for selector in selectors:
        parts = tuple(part.split("=", 1) for part in selector.split(";"))
        if any(len(part) != 2 for part in parts):
            raise RuntimeError("task absence evidence selectors are malformed")
        values = dict(parts)
        if set(values) != {
            "control_instance_id",
            "generation_id",
            "logical_run_id",
            "attempt_generation",
        }:
            raise RuntimeError("task absence evidence selectors are malformed")
        try:
            identities.append(
                CapturedTaskTopologyIdentity(
                    values["control_instance_id"],
                    values["generation_id"],
                    values["logical_run_id"],
                    int(values["attempt_generation"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("task absence evidence selectors are malformed") from exc
    if len(set(identities)) != len(identities):
        raise RuntimeError("task absence evidence selectors are ambiguous")
    return tuple(identities)


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


def _canonical_broker_topology(raw: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    required = {"ready_priority_keys", "unacked_hash", "unacked_index", "unacked_wrapper"}
    if set(raw) != required:
        raise RuntimeError("broker topology is unknown")
    keys = raw["ready_priority_keys"]
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise RuntimeError("broker priority topology is malformed")
    canonical_keys: list[JsonValue] = list(_redis_priority_queue_names("ingestion"))
    if (
        keys != canonical_keys
        or raw["unacked_hash"] != "unacked"
        or raw["unacked_index"] != "unacked_index"
        or raw["unacked_wrapper"] != "kombu-redis-json-v1"
    ):
        raise RuntimeError("broker topology is unsupported")
    return {
        "ready_priority_keys": canonical_keys,
        "unacked_hash": "unacked",
        "unacked_index": "unacked_index",
        "unacked_wrapper": "kombu-redis-json-v1",
    }


def _canonical_broker_observations(
    raw: Mapping[str, tuple[dict[str, JsonValue], ...]],
) -> dict[str, tuple[dict[str, JsonValue], ...]]:
    if set(raw) != {"ready", "unacked"}:
        raise RuntimeError("broker inspection topology is unknown")
    return {
        kind: tuple(sorted(raw[kind], key=lambda item: canonical_json_bytes(item))) for kind in raw
    }


def _has_affected_task(
    observations: Mapping[str, tuple[dict[str, JsonValue], ...]],
    captured_tasks: tuple[CapturedTaskTopologyIdentity, ...],
) -> bool:
    if not captured_tasks:
        return False
    for tasks in observations.values():
        for task in tasks:
            name = task.get("name")
            if not isinstance(name, str):
                raise RuntimeError("task identity is unknown")
            if name not in _AFFECTED_TASKS:
                continue
            identity = _task_identity(task)
            matching = tuple(
                captured
                for captured in captured_tasks
                if (
                    captured.generation_id,
                    captured.logical_run_id,
                    captured.attempt_generation,
                )
                == (identity.generation_id, identity.logical_run_id, identity.attempt_generation)
            )
            if not matching:
                continue
            if identity.control_instance_id is None:
                if any(item.control_instance_id != "legacy-default" for item in matching):
                    raise RuntimeError("legacy task control selector is ambiguous")
                return True
            if any(item.control_instance_id == identity.control_instance_id for item in matching):
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


def _redis_unacked_inventory(
    client: object, hash_key: str, index_key: str
) -> tuple[dict[str, JsonValue], ...]:
    inventory = cast(_RedisInventory, client)
    values = inventory.hgetall(hash_key)
    index = inventory.zrange(index_key, 0, -1)
    normalized_keys = {
        item if isinstance(item, str) else item.decode("utf-8") if isinstance(item, bytes) else None
        for item in values
    }
    normalized_index = {
        item if isinstance(item, str) else item.decode("utf-8") if isinstance(item, bytes) else None
        for item in index
    }
    if None in normalized_keys or normalized_keys != normalized_index:
        raise RuntimeError("broker unacked index/hash topology is inconsistent")
    return tuple(_decode_unacked_delivery(value) for value in values.values())


def _decode_unacked_delivery(value: object) -> dict[str, JsonValue]:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if not isinstance(raw, bytes):
        raise RuntimeError("broker unacked delivery is malformed")
    try:
        wrapper = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("broker unacked delivery is not JSON") from exc
    if not isinstance(wrapper, list) or len(wrapper) != 3 or not isinstance(wrapper[0], str):
        raise RuntimeError("broker unacked wrapper is unsupported")
    return _decode_broker_delivery(wrapper[0])


@dataclass(frozen=True)
class _ObservedTaskIdentity:
    control_instance_id: str | None
    generation_id: str
    logical_run_id: str
    attempt_generation: int


def _task_identity(task: Mapping[str, JsonValue]) -> _ObservedTaskIdentity:
    """Extract task-delivery identities; aliases preserve deployed envelopes."""
    values: list[Mapping[str, JsonValue]] = [task]
    for label in ("kwargs", "headers"):
        nested = task.get(label)
        if nested is not None:
            if not isinstance(nested, dict):
                raise RuntimeError(f"task {label} are malformed")
            values.append(nested)
    control = _single_text(values, ("control_instance_id",), required=False)
    generation = _single_text(values, ("bitrix_generation_id", "generation_id"), required=True)
    logical = _single_text(values, ("bitrix_logical_run_id", "logical_run_id"), required=True)
    attempt = _single_integer(values, ("bitrix_attempt_generation", "attempt_generation"))
    if generation is None or logical is None:
        raise RuntimeError("task topology selector identity is unknown")
    return _ObservedTaskIdentity(control, generation, logical, attempt)


def _single_text(
    values: list[Mapping[str, JsonValue]], keys: tuple[str, ...], *, required: bool
) -> str | None:
    candidates = [value[key] for value in values for key in keys if key in value]
    if not candidates:
        if required:
            raise RuntimeError("task topology selector identity is unknown")
        return None
    if not all(isinstance(candidate, str) and candidate for candidate in candidates):
        raise RuntimeError("task topology selector is malformed")
    first = cast(str, candidates[0])
    if any(candidate != first for candidate in candidates[1:]):
        raise RuntimeError("task topology selector is ambiguous")
    return first


def _single_integer(values: list[Mapping[str, JsonValue]], keys: tuple[str, ...]) -> int:
    candidates = [value[key] for value in values for key in keys if key in value]
    if not candidates:
        raise RuntimeError("task topology selector identity is unknown")
    valid = all(
        isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0
        for candidate in candidates
    )
    if not valid:
        raise RuntimeError("task topology selector is malformed")
    first = cast(int, candidates[0])
    if any(candidate != first for candidate in candidates[1:]):
        raise RuntimeError("task topology selector is ambiguous")
    return first


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
