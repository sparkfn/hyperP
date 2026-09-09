"""Private durable checkpoint for one logical, manifest-gated cleanup run."""

from __future__ import annotations

import json
import re
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.checkpoint_atomic import publish_new, publish_replace, read_bytes
from intelligence.crm.activities.cleanup.receipt import CleanupReceipt
from intelligence.crm.activities.cleanup.types import (
    CHECKPOINT_SCHEMA,
    DISPOSITIONS,
    canonical_digest,
    exact_keys,
    require_count,
    require_identifier,
)
from intelligence.crm.activities.path_safety import (
    confined_directory,
    confined_file,
    has_link_or_reparse,
)

_BINDING = "binding.json"
_STATE = "checkpoint.json"
_ATTEMPTS = "attempts"
_BATCHES = "batches"
_RECONCILIATION = "reconciliation.json"
_ATTEMPT_NAME = re.compile(r"^attempt-[0-9]{8}\.json$")
_BATCH_NAME = re.compile(r"^batch-[0-9]{8}-(intent|result)\.json$")
_SCAN_MAX_SECONDS = 3.0


@dataclass(frozen=True)
class CleanupCheckpoint:
    cleanup_run_id: str
    receipt_digest: str
    receipt: CleanupReceipt
    cursor: int
    batch_count: int
    phase: str


@dataclass(frozen=True)
class DurableOutcomeEvidence:
    """Validated immutable attempt and result evidence for a logical cleanup run."""

    attempts: tuple[str, ...]
    outcomes: tuple[tuple[str, str], ...]
    failure_codes: tuple[tuple[str, str], ...]


def load_bound(root: Path, cleanup_run_id: str) -> CleanupCheckpoint:
    """Load the receipt embedded in binding evidence, then validate the entire checkpoint."""
    _root(root)
    binding = _read(root, _BINDING)
    if binding.get("cleanup_run_id") != cleanup_run_id:
        raise RuntimeError("cleanup checkpoint binding conflicts")
    return load(root, cleanup_run_id, CleanupReceipt.parse(binding.get("receipt")))


def durable_outcomes(root: Path, checkpoint: CleanupCheckpoint) -> DurableOutcomeEvidence:
    """Return typed exact outcome evidence only after the checkpoint validates in full."""
    validated = load(root, checkpoint.cleanup_run_id, checkpoint.receipt)
    attempts = _attempts(root, validated)
    outcomes: list[tuple[str, str]] = []
    codes: list[tuple[str, str]] = []
    for ordinal in range(1, validated.batch_count + 1):
        result = _read(root, _result_name(ordinal))
        raw_outcomes = _outcomes(result.get("outcomes"))
        raw_codes = _outcomes(result.get("failure_codes"))
        for identity in _identity_list(_read(root, _intent_name(ordinal)).get("identities")):
            value = raw_outcomes.get(identity)
            if not isinstance(value, str) or value not in DISPOSITIONS:
                raise RuntimeError("cleanup batch outcome is invalid")
            outcomes.append((identity, value))
        for identity, code in raw_codes.items():
            if not isinstance(code, str) or identity not in raw_outcomes:
                raise RuntimeError("cleanup batch failure code is invalid")
            codes.append((identity, code))
    return DurableOutcomeEvidence(attempts, tuple(outcomes), tuple(sorted(codes)))


def _attempts(root: Path, checkpoint: CleanupCheckpoint) -> tuple[str, ...]:
    directory = root / _ATTEMPTS
    if not directory.exists():
        return ()
    result: list[str] = []
    for ordinal, path in enumerate(sorted(directory.glob("attempt-*.json")), start=1):
        evidence = _read(root, f"{_ATTEMPTS}/{path.name}")
        if (
            evidence.get("schema_version") != CHECKPOINT_SCHEMA
            or evidence.get("ordinal") != ordinal
        ):
            raise RuntimeError("cleanup attempt evidence is corrupt")
        if evidence.get("receipt_digest") != checkpoint.receipt_digest:
            raise RuntimeError("cleanup attempt receipt binding conflicts")
        result.append(require_identifier(evidence.get("attempt_run_id"), "attempt_run_id"))
    return tuple(result)


def checkpoint_root(workspace: Path, cleanup_run_id: str, create: bool = True) -> Path:
    """Return the one persistent root; never place durable evidence in run staging."""
    require_identifier(cleanup_run_id, "cleanup_run_id")
    return confined_directory(workspace, ("cleanup-checkpoints", cleanup_run_id), create=create)


def initialize(root: Path, cleanup_run_id: str, receipt: CleanupReceipt) -> CleanupCheckpoint:
    _root(root)
    require_identifier(cleanup_run_id, "cleanup_run_id")
    if receipt.cleanup_run_id != cleanup_run_id:
        raise RuntimeError("cleanup receipt logical run binding conflicts")
    binding = {
        "schema_version": CHECKPOINT_SCHEMA,
        "cleanup_run_id": cleanup_run_id,
        "receipt": receipt.as_dict(),
        "receipt_digest": receipt.logical_digest,
        "request_digest": canonical_digest(
            {
                "authorization": receipt.authorization.as_dict(),
                "target": receipt.target.as_dict(),
                "batch_size": receipt.batch_size,
                "resource_ceilings": receipt.resource_ceilings.as_dict(),
            }
        ),
    }
    _write_once(root, _BINDING, binding)
    initial = {
        "schema_version": CHECKPOINT_SCHEMA,
        "receipt_digest": receipt.logical_digest,
        "cursor": 0,
        "batch_count": 0,
        "phase": "ready",
    }
    _write_once(root, _STATE, initial)
    return load(root, cleanup_run_id, receipt)


def load(root: Path, cleanup_run_id: str, receipt: CleanupReceipt) -> CleanupCheckpoint:
    if receipt.cleanup_run_id != cleanup_run_id:
        raise RuntimeError("cleanup receipt logical run binding conflicts")
    _scan(root, receipt)
    binding = _read(root, _BINDING)
    expected = {"schema_version", "cleanup_run_id", "receipt", "receipt_digest", "request_digest"}
    exact_keys(binding, frozenset(expected), "cleanup binding")
    if (
        binding["schema_version"] != CHECKPOINT_SCHEMA
        or binding["cleanup_run_id"] != cleanup_run_id
    ):
        raise RuntimeError("cleanup checkpoint binding conflicts")
    if (
        binding["receipt_digest"] != receipt.logical_digest
        or binding["receipt"] != receipt.as_dict()
    ):
        raise RuntimeError("cleanup checkpoint receipt binding conflicts")
    request_digest = canonical_digest(
        {
            "authorization": receipt.authorization.as_dict(),
            "target": receipt.target.as_dict(),
            "batch_size": receipt.batch_size,
            "resource_ceilings": receipt.resource_ceilings.as_dict(),
        }
    )
    if binding["request_digest"] != request_digest:
        raise RuntimeError("cleanup checkpoint request binding conflicts")
    state = _read(root, _STATE)
    exact_keys(
        state,
        frozenset({"schema_version", "receipt_digest", "cursor", "batch_count", "phase"}),
        "cleanup checkpoint",
    )
    if (
        state["schema_version"] != CHECKPOINT_SCHEMA
        or state["receipt_digest"] != receipt.logical_digest
    ):
        raise RuntimeError("cleanup checkpoint state conflicts")
    cursor = require_count(state["cursor"], "cleanup cursor")
    count = require_count(state["batch_count"], "cleanup batch_count")
    phase = state["phase"]
    if phase not in {"ready", "executing", "reconciled"}:
        raise ValueError("cleanup checkpoint phase is invalid")
    if cursor > len(receipt.identities) or count > receipt.resource_ceilings.max_batches:
        raise RuntimeError("cleanup checkpoint cursor exceeds receipt")
    _validate_batches(root, receipt, cursor, count)
    return CleanupCheckpoint(cleanup_run_id, receipt.logical_digest, receipt, cursor, count, phase)


def record_attempt(root: Path, checkpoint: CleanupCheckpoint, attempt_run_id: str) -> None:
    require_identifier(attempt_run_id, "attempt_run_id")
    ordinal = _next_attempt_ordinal(root)
    _write_once(
        root,
        f"{_ATTEMPTS}/attempt-{ordinal:08d}.json",
        {
            "schema_version": CHECKPOINT_SCHEMA,
            "ordinal": ordinal,
            "receipt_digest": checkpoint.receipt_digest,
            "attempt_run_id": attempt_run_id,
        },
    )


def write_batch_intent(
    root: Path, checkpoint: CleanupCheckpoint, ordinal: int, identities: Sequence[str]
) -> None:
    _assert_next(checkpoint, ordinal)
    expected = tuple(
        item.source_record_pk
        for item in checkpoint.receipt.identities[
            checkpoint.cursor : checkpoint.cursor + checkpoint.receipt.batch_size
        ]
    )
    supplied = tuple(identities)
    if supplied != expected or not supplied:
        raise RuntimeError("cleanup batch intent is outside receipt or skips cursor")
    _write_once(
        root,
        _intent_name(ordinal),
        {
            "schema_version": CHECKPOINT_SCHEMA,
            "ordinal": ordinal,
            "receipt_digest": checkpoint.receipt_digest,
            "cursor_before": checkpoint.cursor,
            "identities": list(supplied),
            "identity_digest": canonical_digest(list(supplied)),
        },
    )


def write_batch_result(
    root: Path,
    checkpoint: CleanupCheckpoint,
    ordinal: int,
    outcomes: Mapping[str, str],
    failure_codes: Mapping[str, str],
) -> CleanupCheckpoint:
    _assert_next(checkpoint, ordinal)
    intent = _read(root, _intent_name(ordinal))
    expected = _identity_list(intent.get("identities"))
    if tuple(outcomes) != expected or set(outcomes.values()) - DISPOSITIONS:
        raise RuntimeError("cleanup result is out of set or unordered")
    if set(failure_codes) - set(expected) or any(
        outcomes[key] not in {"failed", "conflict", "retained"} for key in failure_codes
    ):
        raise RuntimeError("cleanup failure codes conflict with outcomes")
    for key, value in failure_codes.items():
        require_identifier(key, "failure identity")
        require_identifier(value, "failure code")
    result = {
        "schema_version": CHECKPOINT_SCHEMA,
        "ordinal": ordinal,
        "receipt_digest": checkpoint.receipt_digest,
        "cursor_before": checkpoint.cursor,
        "cursor_after": checkpoint.cursor + len(expected),
        "outcomes": dict(outcomes),
        "failure_codes": dict(failure_codes),
        "outcome_digest": canonical_digest(
            {"outcomes": dict(outcomes), "failure_codes": dict(failure_codes)}
        ),
    }
    _write_once(root, _result_name(ordinal), result)
    _replace_state(
        root, checkpoint.receipt_digest, checkpoint.cursor + len(expected), ordinal, "executing"
    )
    return load(root, checkpoint.cleanup_run_id, checkpoint.receipt)


def unresolved_batch(root: Path, checkpoint: CleanupCheckpoint) -> int | None:
    ordinal = checkpoint.batch_count + 1
    intent = root / _intent_name(ordinal)
    result = root / _result_name(ordinal)
    if intent.exists() and not result.exists():
        return ordinal
    return None


def remaining_identities(root: Path, checkpoint: CleanupCheckpoint) -> tuple[str, ...]:
    if unresolved_batch(root, checkpoint) is not None:
        raise RuntimeError(
            "cleanup acknowledgement is uncertain; external reconciliation is required"
        )
    return tuple(
        item.source_record_pk for item in checkpoint.receipt.identities[checkpoint.cursor :]
    )


def write_reconciliation(
    root: Path, checkpoint: CleanupCheckpoint, value: Mapping[str, object]
) -> None:
    if checkpoint.cursor != len(checkpoint.receipt.identities):
        raise RuntimeError("cleanup reconciliation cannot precede all durable results")
    _write_once(root, _RECONCILIATION, dict(value))
    _replace_state(
        root, checkpoint.receipt_digest, checkpoint.cursor, checkpoint.batch_count, "reconciled"
    )


def _validate_batches(root: Path, receipt: CleanupReceipt, cursor: int, count: int) -> None:
    offset = 0
    for ordinal in range(1, count + 1):
        intent = _read(root, _intent_name(ordinal))
        result = _read(root, _result_name(ordinal))
        expected = tuple(
            item.source_record_pk
            for item in receipt.identities[offset : offset + receipt.batch_size]
        )
        if (
            intent.get("ordinal") != ordinal
            or intent.get("cursor_before") != offset
            or intent.get("identities") != list(expected)
        ):
            raise RuntimeError("cleanup batch intent is corrupt")
        if result.get("ordinal") != ordinal or result.get("cursor_before") != offset:
            raise RuntimeError("cleanup batch result is corrupt")
        if (
            result.get("cursor_after") != offset + len(expected)
            or tuple(_outcomes(result.get("outcomes"))) != expected
        ):
            raise RuntimeError("cleanup batch cursor or outcomes are corrupt")
        offset += len(expected)
    if offset != cursor:
        raise RuntimeError("cleanup checkpoint cursor regressed or skipped")
    next_intent = root / _intent_name(count + 1)
    if next_intent.exists() and not (root / _result_name(count + 1)).exists():
        _read(root, _intent_name(count + 1))


def _write_once(root: Path, relative: str, value: Mapping[str, object]) -> None:
    path = _path(root, relative)
    payload = canonical_json(dict(value)).encode("utf-8")
    if path.exists() or path.is_symlink():
        if read_bytes(path) != payload:
            raise RuntimeError("immutable cleanup checkpoint evidence conflicts")
        return
    publish_new(path, payload)


def _replace_state(root: Path, digest: str, cursor: int, count: int, phase: str) -> None:
    payload = canonical_json(
        {
            "schema_version": CHECKPOINT_SCHEMA,
            "receipt_digest": digest,
            "cursor": cursor,
            "batch_count": count,
            "phase": phase,
        }
    ).encode("utf-8")
    publish_replace(_path(root, _STATE), payload)


def _read(root: Path, relative: str) -> dict[str, object]:
    raw = read_bytes(_path(root, relative))
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("cleanup checkpoint evidence is corrupt") from error
    if not isinstance(value, dict) or raw != canonical_json(value).encode("utf-8"):
        raise ValueError("cleanup checkpoint evidence is noncanonical")
    return dict(value)


def _path(root: Path, relative: str) -> Path:
    parts = tuple(relative.split("/"))
    if not parts or any(not part or part in {".", ".."} for part in parts):
        raise ValueError("cleanup checkpoint path is unsafe")
    if len(parts) == 1:
        return confined_file(root.parent.parent, ("cleanup-checkpoints", root.name, parts[0]))
    confined_directory(
        root.parent.parent, ("cleanup-checkpoints", root.name, *parts[:-1]), create=True
    )
    return confined_file(root.parent.parent, ("cleanup-checkpoints", root.name, *parts))


def _root(root: Path) -> None:
    if root.parent.name != "cleanup-checkpoints" or not root.name:
        raise ValueError("cleanup checkpoint root is outside Intelligence workspace")
    confined_directory(root.parent.parent, ("cleanup-checkpoints", root.name), create=False)


def _scan(root: Path, receipt: CleanupReceipt) -> None:
    _root(root)
    bytes_used = 0
    entries = 0
    started = time.monotonic()
    stack = [root]
    allowed_dirs = {_ATTEMPTS, _BATCHES}
    while stack:
        directory = stack.pop()
        for child in directory.iterdir():
            if time.monotonic() - started > _SCAN_MAX_SECONDS:
                raise RuntimeError("cleanup checkpoint scan exceeds time ceiling")
            metadata = child.lstat()
            entries += 1
            if entries > receipt.resource_ceilings.max_checkpoint_entries:
                raise RuntimeError("cleanup checkpoint exceeds entry ceiling")
            if (
                has_link_or_reparse(metadata)
                or not stat.S_ISREG(metadata.st_mode)
                and not stat.S_ISDIR(metadata.st_mode)
            ):
                raise ValueError("cleanup checkpoint contains unsafe evidence")
            if stat.S_ISDIR(metadata.st_mode):
                if child.parent != root or child.name not in allowed_dirs:
                    raise ValueError("cleanup checkpoint contains unknown directory")
                stack.append(child)
                continue
            if metadata.st_nlink != 1 or not _allowed_file(child):
                raise ValueError("cleanup checkpoint contains unknown or unsafe file")
            bytes_used += metadata.st_size
            if bytes_used > receipt.resource_ceilings.max_checkpoint_bytes:
                raise RuntimeError("cleanup checkpoint exceeds byte ceiling")


def _next_attempt_ordinal(root: Path) -> int:
    directory = root / _ATTEMPTS
    if not directory.exists():
        return 1
    values = sorted(path.name for path in directory.glob("attempt-*.json"))
    return len(values) + 1


def _intent_name(ordinal: int) -> str:
    if ordinal < 1:
        raise ValueError("batch ordinal is invalid")
    return f"{_BATCHES}/batch-{ordinal:08d}-intent.json"


def _result_name(ordinal: int) -> str:
    if ordinal < 1:
        raise ValueError("batch ordinal is invalid")
    return f"{_BATCHES}/batch-{ordinal:08d}-result.json"


def _assert_next(checkpoint: CleanupCheckpoint, ordinal: int) -> None:
    if (
        ordinal != checkpoint.batch_count + 1
        or ordinal > checkpoint.receipt.resource_ceilings.max_batches
    ):
        raise RuntimeError("cleanup batch ordinal skips durable progress")


def _identity_list(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("cleanup batch identities are corrupt")
    return tuple(value)


def _outcomes(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise RuntimeError("cleanup batch outcomes are corrupt")
    return value


def _allowed_file(path: Path) -> bool:
    if path.parent.name == _ATTEMPTS:
        return _ATTEMPT_NAME.fullmatch(path.name) is not None
    if path.parent.name == _BATCHES:
        return _BATCH_NAME.fullmatch(path.name) is not None
    return path.name in {_BINDING, _STATE, _RECONCILIATION}
