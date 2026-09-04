"""Regression coverage for stdin-safe staging deployment health checks."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import TextIO

_ROOT = Path(__file__).parents[3]
_DEPLOY_SCRIPT = _ROOT / "scripts" / "deploy" / "hyperp-staging.sh"
_EXPECTED_SHA = "a" * 40
_DOCKER_STARTED = "docker-started"
_BASH = Path(r"C:\Program Files\Git\bin\bash.exe") if os.name == "nt" else Path("bash")
_INTERNAL_HEALTH_CODE = (
    "import urllib.request; "
    "urllib.request.urlopen('http://localhost:3000/health', timeout=10).read()"
)


@dataclass
class _Harness:
    process: subprocess.Popen[str]
    output: list[str]
    docker_started: Event
    reader: Thread
    docker_log: Path
    deployed_revision: Path
    deployment_attempt: Path


def _production_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


def _read_output(stream: TextIO, lines: list[str], started: Event) -> None:
    for line in stream:
        lines.append(line)
        if line.rstrip("\n") == _DOCKER_STARTED:
            started.set()


def _write_draining_docker(bin_dir: Path) -> None:
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\\n' "$*" >> "$DOCKER_LOG"
printf 'docker-started\\n'
cat >/dev/null
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)


def _start_harness(tmp_path: Path, internal_health: str, write_revision: str) -> _Harness:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_draining_docker(bin_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    docker_log = tmp_path / "docker.log"
    deployed_revision = data_dir / "deployed-revision"
    deployment_attempt = data_dir / "deployment-attempt"
    deployment_attempt.write_text(f"{_EXPECTED_SHA} previous-revision\n", encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER_LOG": str(docker_log),
        "TEST_DEPLOYED_REVISION_FILE": str(deployed_revision),
        "TEST_DEPLOYMENT_ATTEMPT_FILE": str(deployment_attempt),
    }
    process = subprocess.Popen(
        [str(_BASH), "-s"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    output: list[str] = []
    docker_started = Event()
    reader = Thread(target=_read_output, args=(process.stdout, output, docker_started))
    reader.start()
    head = "\n".join(
        (
            "set -Eeuo pipefail",
            f"EXPECTED_SHA={_EXPECTED_SHA}",
            'DEPLOYED_REVISION_FILE="${TEST_DEPLOYED_REVISION_FILE:?}"',
            'DEPLOYMENT_ATTEMPT_FILE="${TEST_DEPLOYMENT_ATTEMPT_FILE:?}"',
            "COMPOSE=(docker compose -p stdin-test -f compose.yml)",
            "fail() { printf 'failure: %s\\n' \"$*\" >&2; exit 1; }",
            internal_health,
            write_revision,
            "assert_internal_api_health",
            "",
        )
    )
    process.stdin.write(head)
    process.stdin.flush()
    return _Harness(
        process,
        output,
        docker_started,
        reader,
        docker_log,
        deployed_revision,
        deployment_attempt,
    )


def _complete_harness(harness: _Harness) -> str:
    assert harness.process.stdin is not None
    tail = """assert_external_health() { printf 'external-health-ran:%s\\n' "$1"; }
assert_git_sync() { printf 'git-sync-ran\\n'; }
assert_external_health 'https://health.invalid/health'
assert_git_sync
write_deployed_revision
printf 'deployment-terminal-marker\\n'
"""
    harness.process.stdin.write(tail)
    harness.process.stdin.close()
    assert harness.process.wait(timeout=5) == 0
    harness.reader.join(timeout=5)
    assert not harness.reader.is_alive(), "deployment output reader did not finish"
    return "".join(harness.output)


def test_internal_health_check_does_not_consume_streamed_deploy_tail(tmp_path: Path) -> None:
    """Keep a stdin-fed ``bash -s`` deployment alive after the Compose health check."""
    script = _DEPLOY_SCRIPT.read_text(encoding="utf-8")
    harness = _start_harness(
        tmp_path,
        _production_function(script, "assert_internal_api_health"),
        _production_function(script, "write_deployed_revision"),
    )

    try:
        assert harness.docker_started.wait(timeout=5), (
            "fake docker never started the health command"
        )
        output = _complete_harness(harness)
    finally:
        if harness.process.poll() is None:
            harness.process.kill()
            harness.process.wait(timeout=5)
        harness.reader.join(timeout=5)

    assert harness.docker_log.read_text(encoding="utf-8").splitlines() == [
        "compose -p stdin-test -f compose.yml exec -T api python -c " + _INTERNAL_HEALTH_CODE
    ]
    assert "external-health-ran:https://health.invalid/health" in output
    assert "git-sync-ran" in output
    assert harness.deployed_revision.read_text(encoding="utf-8") == f"{_EXPECTED_SHA}\n"
    assert not harness.deployment_attempt.exists()
    assert "deployment-terminal-marker" in output
