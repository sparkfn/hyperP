"""Strict child-process probe transport for CRM repair workload tests.

The workload modules remain executable as scripts for their own child dispatch.  This
helper supplies the fixed interpreter invocation and the only accepted result shape,
keeping the child import path scoped to the ingestion service and avoiding nested pytest.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, TypeGuard

CHILD_PROBE_ARGUMENT: Final = "--crm-deal-identity-repair-child-probe"
PROBE_RESULT_VERSION: Final = 1
LINUX_RSS_SOURCE: Final = "linux-resource-ru_maxrss-bytes"
MAX_CHILD_PEAK_RSS_BYTES: Final = 2 * 1024 * 1024 * 1024
_MAX_METRIC_COUNT: Final = 32
_MAX_METRIC_NAME_LENGTH: Final = 64
_MAX_METRIC_STRING_LENGTH: Final = 256

_TESTS_ROOT = Path(__file__).resolve().parent
_INGESTION_ROOT = _TESTS_ROOT.parent
_REPOSITORY_ROOT = _INGESTION_ROOT.parent.parent
_RESULT_KEYS: Final = frozenset(
    {
        "version",
        "workload",
        "elapsed_seconds",
        "peak_rss_bytes",
        "rss_source",
        "metrics",
    }
)

ProbeMetric = bool | int | str


@dataclass(frozen=True)
class ChildProbeResult:
    """Validated evidence emitted by one clean child interpreter."""

    workload: str
    elapsed_seconds: float
    peak_rss_bytes: int
    metrics: dict[str, ProbeMetric]


def child_probe_requested(argv: Sequence[str], workload: str) -> bool:
    """Return whether a workload module was launched by ``run_child_probe`` exactly."""
    return tuple(argv[1:]) == (CHILD_PROBE_ARGUMENT, workload)


def run_child_probe(module_path: Path, workload: str) -> ChildProbeResult:
    """Run one workload module in a fresh interpreter and validate its evidence."""
    if not module_path.is_file():
        raise ValueError(f"child probe module does not exist: {module_path}")
    if not workload or workload.startswith("-"):
        raise ValueError("child probe workload must be a non-option name")

    command = [sys.executable, str(module_path.resolve()), CHILD_PROBE_ARGUMENT, workload]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        cwd=_REPOSITORY_ROOT,
        env=_child_environment(),
        shell=False,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(_child_failure_message(command, completed))
    try:
        return _parse_child_result(completed.stdout, workload)
    except ValueError as error:
        raise AssertionError(_invalid_result_message(completed, error)) from error


def emit_child_probe_result(
    workload: str,
    elapsed_seconds: float,
    metrics: Mapping[str, ProbeMetric],
) -> None:
    """Write the sole versioned JSON result line after a successful Linux workload."""
    _validate_workload(workload)
    _validate_elapsed_seconds(elapsed_seconds)
    normalized_metrics = _normalized_metrics(metrics)
    payload = {
        "version": PROBE_RESULT_VERSION,
        "workload": workload,
        "elapsed_seconds": elapsed_seconds,
        "peak_rss_bytes": linux_peak_rss_bytes(),
        "rss_source": LINUX_RSS_SOURCE,
        "metrics": normalized_metrics,
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")


def linux_peak_rss_bytes() -> int:
    """Return child-owned Linux high-water RSS, rejecting unavailable evidence."""
    if sys.platform != "linux":
        raise RuntimeError("CRM repair child probes require Linux peak-RSS evidence")
    import resource

    peak_bytes = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    if peak_bytes <= 0:
        raise RuntimeError("CRM repair child probe reported unavailable Linux peak RSS")
    return peak_bytes


def format_probe_evidence(result: ChildProbeResult) -> str:
    """Return concise successful evidence suitable for the parent pytest log."""
    return (
        f"{result.workload}: elapsed={result.elapsed_seconds:.3f}s "
        f"peak_rss={result.peak_rss_bytes}B metrics={len(result.metrics)}"
    )


def format_probe_metrics(result: ChildProbeResult) -> str:
    """Render the bounded complete metric map for a failing parent assertion."""
    return json.dumps(result.metrics, separators=(",", ":"), sort_keys=True)


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(_INGESTION_ROOT)
    return environment


def _parse_child_result(stdout: str, expected_workload: str) -> ChildProbeResult:
    stripped_stdout = stdout.strip()
    if not stripped_stdout:
        raise ValueError("child probe did not emit JSON evidence")
    try:
        decoded: object = json.loads(stripped_stdout)
    except json.JSONDecodeError as error:
        raise ValueError("child probe stdout is not one JSON result") from error
    payload = _object_mapping(decoded, "child probe result")
    if set(payload) != _RESULT_KEYS:
        raise ValueError("child probe result keys are not the versioned contract")
    version = _required_int(payload, "version")
    if version != PROBE_RESULT_VERSION:
        raise ValueError("child probe result version is unsupported")
    workload = _required_string(payload, "workload")
    if workload != expected_workload:
        raise ValueError("child probe result workload does not match the requested workload")
    elapsed_seconds = _required_elapsed_seconds(payload)
    peak_rss_bytes = _required_int(payload, "peak_rss_bytes")
    if peak_rss_bytes <= 0 or peak_rss_bytes >= MAX_CHILD_PEAK_RSS_BYTES:
        raise ValueError("child probe peak RSS is absent or exceeds the 2 GiB contract")
    if _required_string(payload, "rss_source") != LINUX_RSS_SOURCE:
        raise ValueError("child probe did not report the required Linux RSS source")
    metrics = _normalized_metrics(_object_mapping(payload["metrics"], "child probe metrics"))
    if not metrics:
        raise ValueError("child probe metrics must contain workload evidence")
    return ChildProbeResult(workload, elapsed_seconds, peak_rss_bytes, metrics)


def _object_mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be an object")
    mapped: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{description} keys must be strings")
        mapped[key] = item
    return mapped


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"child probe {key} must be an integer")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"child probe {key} must be a non-empty string")
    return value


def _required_elapsed_seconds(payload: Mapping[str, object]) -> float:
    value = payload.get("elapsed_seconds")
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("child probe elapsed_seconds must be numeric")
    elapsed_seconds = float(value)
    _validate_elapsed_seconds(elapsed_seconds)
    return elapsed_seconds


def _validate_workload(workload: str) -> None:
    if not workload or workload.startswith("-"):
        raise ValueError("child probe workload must be a non-option name")


def _validate_elapsed_seconds(elapsed_seconds: float) -> None:
    if not math.isfinite(elapsed_seconds) or elapsed_seconds <= 0:
        raise ValueError("child probe elapsed_seconds must be finite and positive")


def _normalized_metrics(metrics: Mapping[str, ProbeMetric]) -> dict[str, ProbeMetric]:
    if len(metrics) > _MAX_METRIC_COUNT:
        raise ValueError("child probe result has too many metrics")
    normalized: dict[str, ProbeMetric] = {}
    for key, value in metrics.items():
        if (
            not key
            or len(key) > _MAX_METRIC_NAME_LENGTH
            or not _is_probe_metric(value)
            or isinstance(value, str) and len(value) > _MAX_METRIC_STRING_LENGTH
        ):
            raise ValueError("child probe metrics must have named scalar values")
        normalized[key] = value
    return normalized


def _is_probe_metric(value: object) -> TypeGuard[ProbeMetric]:
    return isinstance(value, str | bool) or (
        isinstance(value, int) and not isinstance(value, bool)
    )


def _child_failure_message(
    command: Sequence[str], completed: subprocess.CompletedProcess[str]
) -> str:
    rendered_command = " ".join(command)
    return (
        f"child probe exited {completed.returncode}: {rendered_command}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _invalid_result_message(
    completed: subprocess.CompletedProcess[str], error: ValueError
) -> str:
    return (
        f"child probe emitted invalid evidence: {error}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
