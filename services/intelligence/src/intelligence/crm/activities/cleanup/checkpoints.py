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
from intelligence.crm.activities.cleanup.reconciliation import (
    Reconciliation,
    parse_reconciliation,
    verify_durable_partition,
)
from intelligence.crm.activities.cleanup.types import (
    CHECKPOINT_SCHEMA,
    DISPOSITIONS,
    canonical_digest,
    exact_keys,
    require_count,
    require_digest,
    require_identifier,
    require_mapping,
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
_FAILURE_DISPOSITIONS = frozenset({"failed", "conflict", "retained"})


@dataclass(frozen=True)
class CleanupCheckpoint:
    cleanup_run_id: str
    receipt_digest: str
    receipt: CleanupReceipt
    cursor: int
    batch_count: int
    phase: str


@dataclass(frozen=True)
class BatchIntent:
    """Validated immutable scheduling evidence for one receipt batch."""

    ordinal: int
    receipt_digest: str
    cursor_before: int
    identities: tuple[str, ...]
    identity_digest: str


@dataclass(frozen=True)
class BatchResult:
    """Validated immutable canonical-map result evidence for one scheduled batch."""

    ordinal: int
    receipt_digest: str
    cursor_before: int
    cursor_after: int
    outcomes: tuple[tuple[str, str], ...]
    failure_codes: tuple[tuple[str, str], ...]
    outcome_digest: str

    def outcomes_by_identity(self) -> dict[str, str]:
        """Return lookup evidence without imposing lexical JSON map order on scheduling."""
        return dict(self.outcomes)


@dataclass(frozen=True)
class DurableOutcomeEvidence:
    """Validated immutable attempt and result evidence for a logical cleanup run."""

    attempts: tuple[str, ...]
    outcomes: tuple[tuple[str, str], ...]
    failure_codes: tuple[tuple[str, str], ...]

    def canonical_partition(
        self,
    ) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
        """Return the complete durable partition in canonical lookup order for comparison."""
        return tuple(sorted(self.outcomes)), self.failure_codes


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
    return _durable_outcomes(root, validated)


def load_reconciliation(root: Path, checkpoint: CleanupCheckpoint) -> Reconciliation:
    """Load reconciliation only when it binds this receipt and its durable result partition."""
    validated = load(root, checkpoint.cleanup_run_id, checkpoint.receipt)
    if validated.phase != "reconciled":
        raise RuntimeError("cleanup reconciliation is not durable")
    return _validated_reconciliation(root, validated)


def recover_durable_results(root: Path, checkpoint: CleanupCheckpoint) -> CleanupCheckpoint:
    """Advance a stale cursor from validated immutable evidence without rerunning mutation."""
    current = load(root, checkpoint.cleanup_run_id, checkpoint.receipt)
    while current.phase != "reconciled":
        intent, result = _next_batch_evidence(root, current)
        if result is None:
            return current
        if intent is None:
            raise RuntimeError("cleanup result exists without durable intent")
        _replace_state(
            root,
            current.receipt_digest,
            result.cursor_after,
            result.ordinal,
            "executing",
        )
        current = load(root, current.cleanup_run_id, current.receipt)
    return current


def _durable_outcomes(root: Path, checkpoint: CleanupCheckpoint) -> DurableOutcomeEvidence:
    attempts = _attempts(root, checkpoint)
    outcomes: list[tuple[str, str]] = []
    codes: list[tuple[str, str]] = []
    offset = 0
    for ordinal in range(1, checkpoint.batch_count + 1):
        intent = _read_intent_at(
            root, checkpoint.receipt, checkpoint.receipt_digest, ordinal, offset
        )
        result = parse_batch_result(_read(root, _result_name(ordinal)), intent)
        result_map = result.outcomes_by_identity()
        outcomes.extend((identity, result_map[identity]) for identity in intent.identities)
        codes.extend(result.failure_codes)
        offset = result.cursor_after
    if offset != checkpoint.cursor:
        raise RuntimeError("cleanup durable outcome cursor conflicts")
    return DurableOutcomeEvidence(attempts, tuple(outcomes), tuple(sorted(codes)))


def _attempts(root: Path, checkpoint: CleanupCheckpoint) -> tuple[str, ...]:
    directory = root / _ATTEMPTS
    if not directory.exists():
        return ()
    result: list[str] = []
    for ordinal, path in enumerate(sorted(directory.glob("attempt-*.json")), start=1):
        evidence = require_mapping(_read(root, f"{_ATTEMPTS}/{path.name}"), "cleanup attempt")
        exact_keys(
            evidence,
            frozenset({"schema_version", "ordinal", "receipt_digest", "attempt_run_id"}),
            "cleanup attempt",
        )
        if (
            evidence.get("schema_version") != CHECKPOINT_SCHEMA
            or require_count(evidence.get("ordinal"), "attempt ordinal") != ordinal
        ):
            raise RuntimeError("cleanup attempt evidence is corrupt")
        if (
            require_digest(evidence.get("receipt_digest"), "attempt receipt_digest")
            != checkpoint.receipt_digest
        ):
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
    _validate_binding(_read(root, _BINDING), cleanup_run_id, receipt)
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
    checkpoint = CleanupCheckpoint(
        cleanup_run_id, receipt.logical_digest, receipt, cursor, count, phase
    )
    _validate_batches(root, checkpoint)
    if phase == "reconciled":
        if cursor != len(receipt.identities):
            raise RuntimeError("reconciled checkpoint cursor is incomplete")
        _validated_reconciliation(root, checkpoint)
    return checkpoint


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
    intent = _read_intent(root, checkpoint, ordinal)
    result = _result_from_values(intent, outcomes, failure_codes)
    _write_once(root, _result_name(ordinal), _result_dict(result))
    _replace_state(root, checkpoint.receipt_digest, result.cursor_after, ordinal, "executing")
    return load(root, checkpoint.cleanup_run_id, checkpoint.receipt)


def unresolved_batch(root: Path, checkpoint: CleanupCheckpoint) -> int | None:
    """Return a rigorously validated intent without a result; result-ahead recovery is separate."""
    current = load(root, checkpoint.cleanup_run_id, checkpoint.receipt)
    intent, result = _next_batch_evidence(root, current)
    if intent is not None and result is None:
        return intent.ordinal
    return None


def remaining_identities(root: Path, checkpoint: CleanupCheckpoint) -> tuple[str, ...]:
    current = recover_durable_results(root, checkpoint)
    if unresolved_batch(root, current) is not None:
        raise RuntimeError(
            "cleanup acknowledgement is uncertain; external reconciliation is required"
        )
    return tuple(item.source_record_pk for item in current.receipt.identities[current.cursor :])


def write_reconciliation(
    root: Path, checkpoint: CleanupCheckpoint, value: Mapping[str, object]
) -> None:
    if checkpoint.cursor != len(checkpoint.receipt.identities):
        raise RuntimeError("cleanup reconciliation cannot precede all durable results")
    parsed = parse_reconciliation(
        value,
        tuple(item.source_record_pk for item in checkpoint.receipt.identities),
        checkpoint.receipt_digest,
    )
    evidence = durable_outcomes(root, checkpoint)
    verify_durable_partition(
        parsed, checkpoint.receipt_digest, evidence.outcomes, evidence.failure_codes
    )
    _write_once(root, _RECONCILIATION, parsed.as_dict())
    _replace_state(
        root, checkpoint.receipt_digest, checkpoint.cursor, checkpoint.batch_count, "reconciled"
    )


def _validate_batches(root: Path, checkpoint: CleanupCheckpoint) -> None:
    offset = 0
    for ordinal in range(1, checkpoint.batch_count + 1):
        intent = _read_intent_at(
            root, checkpoint.receipt, checkpoint.receipt_digest, ordinal, offset
        )
        result = parse_batch_result(_read(root, _result_name(ordinal)), intent)
        offset = result.cursor_after
    if offset != checkpoint.cursor:
        raise RuntimeError("cleanup checkpoint cursor regressed or skipped")
    _next_batch_evidence(root, checkpoint)
    _validate_batch_file_inventory(root, checkpoint)


def parse_batch_intent(
    value: object,
    receipt_digest: str,
    ordinal: int,
    cursor_before: int,
    scheduled_identities: Sequence[str],
) -> BatchIntent:
    """Parse one canonical intent and bind it exactly to its receipt cursor and schedule."""
    raw = require_mapping(value, "cleanup batch intent")
    exact_keys(
        raw,
        frozenset(
            {
                "schema_version",
                "ordinal",
                "receipt_digest",
                "cursor_before",
                "identities",
                "identity_digest",
            }
        ),
        "cleanup batch intent",
    )
    identities = _identity_list(raw["identities"])
    digest = require_digest(raw["identity_digest"], "batch identity_digest")
    if (
        raw["schema_version"] != CHECKPOINT_SCHEMA
        or require_count(raw["ordinal"], "batch ordinal") != ordinal
        or require_digest(raw["receipt_digest"], "batch receipt_digest") != receipt_digest
        or require_count(raw["cursor_before"], "batch cursor_before") != cursor_before
        or identities != tuple(scheduled_identities)
        or digest != canonical_digest(list(identities))
    ):
        raise RuntimeError("cleanup batch intent conflicts with durable receipt schedule")
    return BatchIntent(ordinal, receipt_digest, cursor_before, identities, digest)


def parse_batch_result(value: object, intent: BatchIntent) -> BatchResult:
    """Parse a canonical result map while validating membership independently of map ordering."""
    raw = require_mapping(value, "cleanup batch result")
    exact_keys(
        raw,
        frozenset(
            {
                "schema_version",
                "ordinal",
                "receipt_digest",
                "cursor_before",
                "cursor_after",
                "outcomes",
                "failure_codes",
                "outcome_digest",
            }
        ),
        "cleanup batch result",
    )
    outcomes = _outcome_items(raw["outcomes"], "outcomes")
    codes = _outcome_items(raw["failure_codes"], "failure_codes")
    outcome_map = dict(outcomes)
    code_map = dict(codes)
    digest = require_digest(raw["outcome_digest"], "batch outcome_digest")
    if (
        raw["schema_version"] != CHECKPOINT_SCHEMA
        or require_count(raw["ordinal"], "batch ordinal") != intent.ordinal
        or require_digest(raw["receipt_digest"], "batch receipt_digest") != intent.receipt_digest
        or require_count(raw["cursor_before"], "batch cursor_before") != intent.cursor_before
        or require_count(raw["cursor_after"], "batch cursor_after")
        != intent.cursor_before + len(intent.identities)
        or set(outcome_map) != set(intent.identities)
        or set(outcome_map.values()) - DISPOSITIONS
        or set(code_map)
        != {identity for identity, disposition in outcomes if disposition in _FAILURE_DISPOSITIONS}
        or digest != canonical_digest({"outcomes": outcome_map, "failure_codes": code_map})
    ):
        raise RuntimeError("cleanup batch result conflicts with durable intent")
    return BatchResult(
        intent.ordinal,
        intent.receipt_digest,
        intent.cursor_before,
        intent.cursor_before + len(intent.identities),
        outcomes,
        codes,
        digest,
    )


def _validated_reconciliation(root: Path, checkpoint: CleanupCheckpoint) -> Reconciliation:
    parsed = parse_reconciliation(
        _read(root, _RECONCILIATION),
        tuple(item.source_record_pk for item in checkpoint.receipt.identities),
        checkpoint.receipt_digest,
    )
    evidence = _durable_outcomes(root, checkpoint)
    verify_durable_partition(
        parsed, checkpoint.receipt_digest, evidence.outcomes, evidence.failure_codes
    )
    return parsed


def _validate_binding(value: object, cleanup_run_id: str, receipt: CleanupReceipt) -> None:
    binding = require_mapping(value, "cleanup binding")
    exact_keys(
        binding,
        frozenset(
            {"schema_version", "cleanup_run_id", "receipt", "receipt_digest", "request_digest"}
        ),
        "cleanup binding",
    )
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
    if binding["request_digest"] != _request_digest(receipt):
        raise RuntimeError("cleanup checkpoint request binding conflicts")


def _request_digest(receipt: CleanupReceipt) -> str:
    return canonical_digest(
        {
            "authorization": receipt.authorization.as_dict(),
            "target": receipt.target.as_dict(),
            "batch_size": receipt.batch_size,
            "resource_ceilings": receipt.resource_ceilings.as_dict(),
        }
    )


def _next_batch_evidence(
    root: Path, checkpoint: CleanupCheckpoint
) -> tuple[BatchIntent | None, BatchResult | None]:
    ordinal = checkpoint.batch_count + 1
    intent_path = root / _intent_name(ordinal)
    result_path = root / _result_name(ordinal)
    if not intent_path.exists() and not result_path.exists():
        return None, None
    if not intent_path.exists():
        raise RuntimeError("cleanup result exists without durable intent")
    intent = _read_intent_at(
        root, checkpoint.receipt, checkpoint.receipt_digest, ordinal, checkpoint.cursor
    )
    if not result_path.exists():
        return intent, None
    return intent, parse_batch_result(_read(root, _result_name(ordinal)), intent)


def _validate_batch_file_inventory(root: Path, checkpoint: CleanupCheckpoint) -> None:
    directory = root / _BATCHES
    if not directory.exists():
        return
    expected_maximum = checkpoint.batch_count + 1
    maximum_batches = _batch_total(checkpoint.receipt)
    for path in directory.glob("batch-*.json"):
        match = _BATCH_NAME.fullmatch(path.name)
        if match is None:
            raise RuntimeError("cleanup batch evidence name is invalid")
        ordinal = int(path.name[6:14])
        if ordinal > expected_maximum or ordinal > maximum_batches:
            raise RuntimeError("cleanup batch evidence skips durable progress")
    if checkpoint.batch_count == maximum_batches and _next_batch_evidence(root, checkpoint) != (
        None,
        None,
    ):
        raise RuntimeError("cleanup checkpoint has evidence beyond receipt")


def _read_intent(root: Path, checkpoint: CleanupCheckpoint, ordinal: int) -> BatchIntent:
    return _read_intent_at(
        root, checkpoint.receipt, checkpoint.receipt_digest, ordinal, checkpoint.cursor
    )


def _read_intent_at(
    root: Path,
    receipt: CleanupReceipt,
    receipt_digest: str,
    ordinal: int,
    cursor_before: int,
) -> BatchIntent:
    return parse_batch_intent(
        _read(root, _intent_name(ordinal)),
        receipt_digest,
        ordinal,
        cursor_before,
        _scheduled_identities(receipt, cursor_before),
    )


def _result_from_values(
    intent: BatchIntent, outcomes: Mapping[str, str], failure_codes: Mapping[str, str]
) -> BatchResult:
    outcome_items = _outcome_items(dict(outcomes), "outcomes")
    code_items = _outcome_items(dict(failure_codes), "failure_codes")
    value: dict[str, object] = {
        "schema_version": CHECKPOINT_SCHEMA,
        "ordinal": intent.ordinal,
        "receipt_digest": intent.receipt_digest,
        "cursor_before": intent.cursor_before,
        "cursor_after": intent.cursor_before + len(intent.identities),
        "outcomes": dict(outcome_items),
        "failure_codes": dict(code_items),
    }
    value["outcome_digest"] = canonical_digest(
        {"outcomes": value["outcomes"], "failure_codes": value["failure_codes"]}
    )
    return parse_batch_result(value, intent)


def _result_dict(result: BatchResult) -> dict[str, object]:
    return {
        "schema_version": CHECKPOINT_SCHEMA,
        "ordinal": result.ordinal,
        "receipt_digest": result.receipt_digest,
        "cursor_before": result.cursor_before,
        "cursor_after": result.cursor_after,
        "outcomes": dict(result.outcomes),
        "failure_codes": dict(result.failure_codes),
        "outcome_digest": result.outcome_digest,
    }


def _scheduled_identities(receipt: CleanupReceipt, cursor: int) -> tuple[str, ...]:
    return tuple(
        item.source_record_pk for item in receipt.identities[cursor : cursor + receipt.batch_size]
    )


def _batch_total(receipt: CleanupReceipt) -> int:
    return (len(receipt.identities) + receipt.batch_size - 1) // receipt.batch_size


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
    if not isinstance(value, list):
        raise RuntimeError("cleanup batch identities are corrupt")
    identities = tuple(require_identifier(item, "cleanup batch identity") for item in value)
    if not identities or len(set(identities)) != len(identities):
        raise RuntimeError("cleanup batch identities are invalid")
    return identities


def _outcome_items(value: object, field: str) -> tuple[tuple[str, str], ...]:
    raw = require_mapping(value, f"cleanup batch {field}")
    items = tuple(
        sorted(
            (require_identifier(key, field), require_identifier(item, field))
            for key, item in raw.items()
        )
    )
    if len({key for key, _ in items}) != len(items):
        raise RuntimeError(f"cleanup batch {field} are duplicated")
    return items


def _allowed_file(path: Path) -> bool:
    if path.parent.name == _ATTEMPTS:
        return _ATTEMPT_NAME.fullmatch(path.name) is not None
    if path.parent.name == _BATCHES:
        return _BATCH_NAME.fullmatch(path.name) is not None
    return path.name in {_BINDING, _STATE, _RECONCILIATION}
