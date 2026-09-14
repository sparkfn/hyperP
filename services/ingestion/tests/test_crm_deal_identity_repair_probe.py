"""Linux regression coverage for isolated CRM repair child RSS evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from _crm_deal_identity_repair_probe import LINUX_RSS_SOURCE


_MEBIBYTE: Final = 1024 * 1024
_LAUNCHER_ALLOCATION_BYTES: Final = 64 * _MEBIBYTE
_MINIMUM_LAUNCHER_PEAK_BYTES: Final = 48 * _MEBIBYTE
_MINIMUM_PEAK_REDUCTION_BYTES: Final = 32 * _MEBIBYTE
_POST_EXEC_RESULT_KEYS: Final = frozenset(
    {
        "child_peak_rss_bytes",
        "launcher_peak_rss_bytes",
        "rss_source",
    }
)
_LAUNCHER_PROGRAM: Final = "\n".join(
    (
        "import os",
        "import sys",
        "from _crm_deal_identity_repair_probe import linux_peak_rss_bytes",
        "allocation = bytearray(int(sys.argv[1]))",
        "for offset in range(0, len(allocation), 4096):",
        "    allocation[offset] = 1",
        "os.environ['CRM_REPAIR_PROBE_LAUNCHER_PEAK_RSS_BYTES'] = str(linux_peak_rss_bytes())",
        "os.execv(sys.executable, (sys.executable, '-c', sys.argv[2]))",
    )
)
_POST_EXEC_PROGRAM: Final = "\n".join(
    (
        "import json",
        "import os",
        "from _crm_deal_identity_repair_probe import (",
        "    LINUX_RSS_SOURCE,",
        "    linux_peak_rss_bytes,",
        ")",
        "payload = {",
        "    'launcher_peak_rss_bytes': int(",
        "        os.environ['CRM_REPAIR_PROBE_LAUNCHER_PEAK_RSS_BYTES']",
        "    ),",
        "    'child_peak_rss_bytes': linux_peak_rss_bytes(),",
        "    'rss_source': LINUX_RSS_SOURCE,",
        "}",
        "print(json.dumps(payload, separators=(',', ':'), sort_keys=True))",
    )
)


@dataclass(frozen=True)
class _PostExecPeakEvidence:
    """Parsed current-mm peaks from the launcher and its exec replacement."""

    launcher_peak_rss_bytes: int
    child_peak_rss_bytes: int


def test_linux_vmhwm_excludes_launcher_peak_after_exec() -> None:
    """A post-exec VmHWM must not inherit the launcher's touched address space."""
    if sys.platform != "linux":
        raise AssertionError("CRM repair current-mm RSS regression requires Linux VmHWM evidence")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _LAUNCHER_PROGRAM,
            str(_LAUNCHER_ALLOCATION_BYTES),
            _POST_EXEC_PROGRAM,
        ],
        check=False,
        capture_output=True,
        env=_subprocess_environment(),
        shell=False,
        text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(_subprocess_failure_message(completed))
    try:
        evidence = _parse_post_exec_evidence(completed.stdout)
    except ValueError as error:
        raise AssertionError(_invalid_evidence_message(completed, error)) from error

    output = _subprocess_output_message(completed)
    assert evidence.launcher_peak_rss_bytes >= _MINIMUM_LAUNCHER_PEAK_BYTES, output
    assert evidence.child_peak_rss_bytes < evidence.launcher_peak_rss_bytes // 2, output
    assert (
        evidence.launcher_peak_rss_bytes - evidence.child_peak_rss_bytes
        >= _MINIMUM_PEAK_REDUCTION_BYTES
    ), output


def _subprocess_environment() -> dict[str, str]:
    environment = dict(os.environ)
    tests_directory = str(Path(__file__).resolve().parent)
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        tests_directory
        if not existing_python_path
        else os.pathsep.join((tests_directory, existing_python_path))
    )
    return environment


def _parse_post_exec_evidence(stdout: str) -> _PostExecPeakEvidence:
    stripped_stdout = stdout.strip()
    if not stripped_stdout:
        raise ValueError("post-exec probe did not emit JSON evidence")
    try:
        decoded: object = json.loads(stripped_stdout)
    except json.JSONDecodeError as error:
        raise ValueError("post-exec probe stdout is not one JSON result") from error
    payload = _object_mapping(decoded)
    if set(payload) != _POST_EXEC_RESULT_KEYS:
        raise ValueError("post-exec probe keys are not the versioned contract")
    if _required_string(payload, "rss_source") != LINUX_RSS_SOURCE:
        raise ValueError("post-exec probe did not report the required Linux RSS source")
    return _PostExecPeakEvidence(
        launcher_peak_rss_bytes=_required_positive_int(payload, "launcher_peak_rss_bytes"),
        child_peak_rss_bytes=_required_positive_int(payload, "child_peak_rss_bytes"),
    )


def _object_mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("post-exec probe result must be an object")
    payload: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError("post-exec probe result keys must be strings")
        payload[key] = item
    return payload


def _required_positive_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"post-exec probe {key} must be a positive integer")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"post-exec probe {key} must be a non-empty string")
    return value


def _subprocess_failure_message(completed: subprocess.CompletedProcess[str]) -> str:
    return (
        f"post-exec RSS probe exited {completed.returncode}\n"
        f"{_subprocess_output_message(completed)}"
    )


def _subprocess_output_message(completed: subprocess.CompletedProcess[str]) -> str:
    return f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"


def _invalid_evidence_message(
    completed: subprocess.CompletedProcess[str],
    error: ValueError,
) -> str:
    return (
        f"post-exec RSS probe emitted invalid evidence: {error}\n"
        f"{_subprocess_output_message(completed)}"
    )
