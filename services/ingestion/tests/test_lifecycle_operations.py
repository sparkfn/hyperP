"""Deployment and operator-control contracts for lifecycle work."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch

_ROOT = Path(__file__).parents[3]
_CONTROL_SCRIPT = _ROOT / "scripts/lifecycle-worker-control.sh"
_DEPLOY_GUARD = _ROOT / "scripts/lifecycle-worker-deploy-guard.sh"


def _fake_docker(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "docker.log"
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s|%s\\n' "$PWD" "$*" >> "$DOCKER_LOG"
if [[ ${FAIL_DOCKER:-false} == true ]]; then
  exit 42
fi
if [[ "$*" == *"up -d --no-deps lifecycle-worker"* ]]; then
  : > "$CONSUMER_STATE"
fi
if [[ "$*" == *"stop lifecycle-worker"* ]]; then
  rm -f "$CONSUMER_STATE"
fi
if [[ "$*" == *"ps -q lifecycle-worker"* ]] && \
  { [[ ${CONSUMER_RUNNING:-false} == true ]] || [[ -f "$CONSUMER_STATE" ]]; }
then
  printf 'lifecycle-container\\n'
fi
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    return bin_dir, log_path


def _run_control(
    tmp_path: Path,
    command: str,
    *,
    fail_docker: bool = False,
    consumer_running: bool = False,
) -> subprocess.CompletedProcess[str]:
    repo_dir = tmp_path / "staging-checkout"
    repo_dir.mkdir(exist_ok=True)
    bin_dir, log_path = _fake_docker(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DOCKER_LOG": str(log_path),
        "CONSUMER_STATE": str(tmp_path / "consumer.running"),
        "STAGING_REPO_DIR": str(repo_dir),
        "STAGING_COMPOSE_FILE": "host/docker-compose.yml",
        "FAIL_DOCKER": str(fail_docker).lower(),
        "CONSUMER_RUNNING": str(consumer_running).lower(),
    }
    return subprocess.run(
        [str(_CONTROL_SCRIPT), command],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_pause_stops_worker_before_persisting_marker_and_uses_repo_directory(
    tmp_path: Path,
) -> None:
    result = _run_control(tmp_path, "pause")
    repo_dir = tmp_path / "staging-checkout"

    assert result.returncode == 0
    assert (repo_dir / ".lifecycle-worker-paused").is_file()
    assert (tmp_path / "docker.log").read_text(encoding="utf-8").splitlines() == [
        f"{repo_dir}|compose -p stg-hyperp -f host/docker-compose.yml stop lifecycle-worker",
        f"{repo_dir}|compose -p stg-hyperp -f host/docker-compose.yml ps -q lifecycle-worker",
    ]


def test_failed_pause_does_not_create_marker(tmp_path: Path) -> None:
    result = _run_control(tmp_path, "pause", fail_docker=True)

    assert result.returncode == 42
    assert not (tmp_path / "staging-checkout/.lifecycle-worker-paused").exists()


def test_failed_resume_preserves_marker(tmp_path: Path) -> None:
    marker = tmp_path / "staging-checkout/.lifecycle-worker-paused"
    marker.parent.mkdir()
    marker.touch()

    result = _run_control(tmp_path, "resume", fail_docker=True)

    assert result.returncode == 42
    assert marker.is_file()


def test_successful_resume_removes_marker(tmp_path: Path) -> None:
    marker = tmp_path / "staging-checkout/.lifecycle-worker-paused"
    marker.parent.mkdir()
    marker.touch()

    result = _run_control(tmp_path, "resume")

    assert result.returncode == 0
    assert not marker.exists()


def test_status_reports_marker_and_consumer_state(tmp_path: Path) -> None:
    marker = tmp_path / "staging-checkout/.lifecycle-worker-paused"
    marker.parent.mkdir()
    marker.touch()

    result = _run_control(tmp_path, "status", consumer_running=True)

    assert result.returncode == 0
    assert result.stdout.splitlines() == ["pause_marker=present", "consumer_running=true"]


def test_status_does_not_misreport_compose_failure_as_stopped(tmp_path: Path) -> None:
    result = _run_control(tmp_path, "status", fail_docker=True)

    assert result.returncode == 42
    assert "consumer_running=false" not in result.stdout


def test_staging_workflow_uses_testable_lifecycle_deploy_guard() -> None:
    workflow = (_ROOT / ".github/workflows/deploy-staging.yml").read_text(encoding="utf-8")

    assert (
        'COMPOSE="docker compose -p stg-hyperp -f .docker/staging/docker-compose.yml"' in workflow
    )
    assert 'SERVICES="$BUILD_SERVICES"' in workflow
    build = workflow.index("$COMPOSE build $SERVICES")
    preflight = workflow.index("$COMPOSE run --rm --no-deps ingestion-worker", build)
    recreate = workflow.index(
        "$COMPOSE up -d --no-deps --force-recreate $RECREATE_SERVICES",
        preflight,
    )
    postflight = workflow.index("$COMPOSE run --rm --no-deps ingestion-worker", recreate)
    assert build < preflight < recreate < postflight
    assert workflow.count("python -m src.person_completeness_control check") == 2
    assert "python -m src.person_completeness_control backfill" not in workflow
    assert "$COMPOSE stop lifecycle-worker" in workflow
    assert "lifecycle-worker-deploy-guard.sh" in workflow
    assert 'plan "$LIFECYCLE_PAUSED" $SERVICES' in workflow
    assert "verify-paused .docker/staging/docker-compose.yml" in workflow
    control = (_ROOT / "scripts/lifecycle-worker-control.sh").read_text(encoding="utf-8")
    deploy_guard = (_ROOT / "scripts/lifecycle-worker-deploy-guard.sh").read_text(encoding="utf-8")
    for script in (control, deploy_guard):
        assert "STAGING_COMPOSE_PROJECT:-stg-hyperp" in script
        assert 'docker compose -p "$compose_project" -f "$compose_file"' in script


def test_paused_deploy_plan_builds_lifecycle_without_recreating_it() -> None:
    result = subprocess.run(
        [
            str(_DEPLOY_GUARD),
            "plan",
            "true",
            "api",
            "lifecycle-worker",
            "beat",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "BUILD_SERVICES=api\\ lifecycle-worker\\ beat",
        "RECREATE_SERVICES=api\\ beat",
    ]


def _run_deploy_guard(
    tmp_path: Path,
    *,
    container_ids: str = "",
    inspect_running: str = "false",
    fail_ps: bool = False,
    fail_inspect: bool = False,
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "guard-bin"
    bin_dir.mkdir(exist_ok=True)
    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$*" == *"ps -aq lifecycle-worker"* ]]; then
  [[ ${FAIL_PS:-false} != true ]] || exit 41
  printf '%s' "${CONTAINER_IDS:-}"
  exit 0
fi
if [[ "$1" == inspect ]]; then
  [[ ${FAIL_INSPECT:-false} != true ]] || exit 42
  printf '%s\n' "${INSPECT_RUNNING:-false}"
  exit 0
fi
exit 43
""",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CONTAINER_IDS": container_ids,
        "INSPECT_RUNNING": inspect_running,
        "FAIL_PS": str(fail_ps).lower(),
        "FAIL_INSPECT": str(fail_inspect).lower(),
    }
    return subprocess.run(
        [str(_DEPLOY_GUARD), "verify-paused", "compose.yml"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("options", "expected_message"),
    [
        ({"fail_ps": True}, ""),
        ({"container_ids": "one\ntwo\n"}, "Expected at most one"),
        ({"container_ids": "one\n", "fail_inspect": True}, ""),
        (
            {"container_ids": "one\n", "inspect_running": "true"},
            "running despite the deliberate pause marker",
        ),
    ],
)
def test_paused_deploy_guard_fails_closed(
    tmp_path: Path,
    options: dict[str, object],
    expected_message: str,
) -> None:
    result = _run_deploy_guard(tmp_path, **options)  # type: ignore[arg-type]

    assert result.returncode != 0
    if expected_message:
        assert expected_message in result.stderr


def test_paused_deploy_guard_accepts_missing_or_stopped_container(tmp_path: Path) -> None:
    missing = _run_deploy_guard(tmp_path)
    stopped = _run_deploy_guard(tmp_path, container_ids="one\n", inspect_running="false")

    assert missing.returncode == 0
    assert stopped.returncode == 0
    assert "deliberate pause preserved" in stopped.stdout


def test_operations_doc_defines_pause_recovery_and_wait_slo() -> None:
    operations = (_ROOT / "docs/profile-unifier-ingestion-operations.md").read_text(
        encoding="utf-8"
    )

    for command in (
        "scripts/lifecycle-worker-control.sh pause",
        "scripts/lifecycle-worker-control.sh status",
        "scripts/lifecycle-worker-control.sh resume",
        "python -m src.lifecycle_queue_admin status",
        "clear-knows --phase contacts --expected-owner",
        "clear-reconciliation --expected-owner",
    ):
        assert command in operations
    assert "consumer_running=false" in operations
    assert "builds an updated lifecycle image while paused" in operations
    assert "Exit code `1`" in operations
    assert "Exit code `2`" in operations
    assert "after **5 seconds**" in operations
    assert "operational warning, not a hard timeout" in operations


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str | bytes] = {}
        self.fail = False

    def get(self, key: str) -> str | bytes | None:
        if self.fail:
            import redis

            raise redis.ConnectionError("redis unavailable")
        return self.values.get(key)

    def eval(self, _script: str, _keys: int, key: str, expected_owner: str) -> int:
        if self.fail:
            import redis

            raise redis.ConnectionError("redis unavailable")
        current = self.values.get(key)
        if isinstance(current, bytes):
            current = current.decode("utf-8")
        owner = current.split("|", 2)[2] if current and "|" in current else current
        if owner != expected_owner:
            return 0
        del self.values[key]
        return 1


def test_queue_admin_status_normalizes_bytes(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    from src import lifecycle_queue_admin

    client = _FakeRedis()
    client.values[lifecycle_queue_admin._RECONCILIATION_GATE_KEY] = b"reconcile-id"
    client.values[lifecycle_queue_admin._knows_gate_key("contacts")] = b"contacts-id"
    monkeypatch.setattr(lifecycle_queue_admin, "_redis_client", lambda: client)

    assert lifecycle_queue_admin.main(["status"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "reconciliation_state=queued",
        "reconciliation_owner=reconcile-id",
        "knows_contacts_state=queued",
        "knows_contacts_owner=contacts-id",
        "knows_chat_relationships_state=absent",
        "knows_chat_relationships_owner=none",
    ]


def test_queue_admin_owner_checked_clear(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    from src import lifecycle_queue_admin

    client = _FakeRedis()
    key = lifecycle_queue_admin._knows_gate_key("contacts")
    client.values[key] = "current-owner"
    monkeypatch.setattr(lifecycle_queue_admin, "_redis_client", lambda: client)

    assert (
        lifecycle_queue_admin.main(
            ["clear-knows", "--phase", "contacts", "--expected-owner", "stale-owner"]
        )
        == 1
    )
    assert client.values[key] == "current-owner"
    assert "owner did not match" in capsys.readouterr().err

    assert (
        lifecycle_queue_admin.main(
            ["clear-knows", "--phase", "contacts", "--expected-owner", "current-owner"]
        )
        == 0
    )
    assert key not in client.values
    assert capsys.readouterr().out == "gate cleared\n"


def test_queue_admin_normalizes_and_clears_publishing_owner(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    from src import lifecycle_queue_admin

    client = _FakeRedis()
    key = lifecycle_queue_admin._knows_gate_key("contacts")
    client.values[key] = "publishing|1000|next-task"
    monkeypatch.setattr(lifecycle_queue_admin, "_redis_client", lambda: client)

    assert lifecycle_queue_admin.main(["status"]) == 0
    status_lines = capsys.readouterr().out.splitlines()
    assert "knows_contacts_state=publishing" in status_lines
    assert "knows_contacts_owner=next-task" in status_lines
    assert "knows_contacts_publishing_at=1000" in status_lines
    assert (
        lifecycle_queue_admin.main(
            ["clear-knows", "--phase", "contacts", "--expected-owner", "next-task"]
        )
        == 0
    )
    assert key not in client.values


def test_queue_admin_reports_backend_failure_separately(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    from src import lifecycle_queue_admin

    client = _FakeRedis()
    client.fail = True
    monkeypatch.setattr(lifecycle_queue_admin, "_redis_client", lambda: client)

    assert lifecycle_queue_admin.main(["status"]) == 2
    assert "queue gate backend error: ConnectionError" in capsys.readouterr().err


def test_queue_admin_reports_malformed_gate_without_inventing_an_owner(
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    from src import lifecycle_queue_admin

    client = _FakeRedis()
    client.values[lifecycle_queue_admin._knows_gate_key("contacts")] = "publishing|bad|owner"
    monkeypatch.setattr(lifecycle_queue_admin, "_redis_client", lambda: client)

    assert lifecycle_queue_admin.main(["status"]) == 0
    status_lines = capsys.readouterr().out.splitlines()
    assert "knows_contacts_state=malformed" in status_lines
    assert "knows_contacts_owner=none" in status_lines


def test_queue_admin_rejects_invalid_phase(monkeypatch: MonkeyPatch) -> None:
    from src import lifecycle_queue_admin

    monkeypatch.setattr(lifecycle_queue_admin, "_redis_client", _FakeRedis)
    with pytest.raises(SystemExit) as raised:
        lifecycle_queue_admin.main(
            ["clear-knows", "--phase", "invalid", "--expected-owner", "owner"]
        )
    assert raised.value.code == 2
