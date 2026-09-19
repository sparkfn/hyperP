"""Cheap deployment-policy and dormant-runtime contracts for Issue #439."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from importlib.util import module_from_spec, spec_from_file_location
from itertools import combinations
from pathlib import Path

import pytest
from src.ingestion_config import load_ingestion_config

_ROOT = Path(__file__).parents[3]
_POLICY_HELPER = _ROOT / "scripts" / "deploy" / "scheduled_ingestion_policy.py"
_DEPLOY = _ROOT / "scripts" / "deploy" / "hyperp-staging.sh"
_GUARD = _ROOT / "scripts" / "lifecycle-worker-deploy-guard.sh"
_CONTROL = _ROOT / "scripts" / "worker-control.sh"
_COMPOSES = (_ROOT / "docker-compose.yml", _ROOT / ".docker/staging" / "docker-compose.yml")
_WORKERS = ("ingestion-worker", "lifecycle-worker", "beat")


def _service(root: Path, name: str) -> dict[str, object]:
    command: list[str] = ["celery", "worker"]
    if name == "ingestion-worker":
        command.append("--concurrency=1")
        cpus, memory, grace = "1.0", "2G", "300s"
    elif name == "lifecycle-worker":
        command.append("--concurrency=1")
        cpus, memory, grace = "0.5", "1G", "300s"
    else:
        command = ["celery", "beat"]
        cpus, memory, grace = "0.25", "256M", "30s"
    return {
        "cpus": cpus,
        "mem_limit": memory,
        "stop_grace_period": grace,
        "command": command,
        "environment": {
            "INGESTION_CONFIG_FILE": "/app/config/ingestion-config.json",
            "SCHEDULED_INGESTION_TIMEZONE": "Asia/Singapore",
            "SCHEDULED_INGESTION_LOCAL_OPENING": "09:00",
            "SCHEDULED_INGESTION_LOCAL_CUTOFF": "23:00",
            "SCHEDULED_INGESTION_DRAIN_RESERVE_SECONDS": "900",
        },
        "volumes": [{"type": "bind", "source": str(root), "target": "/app/config"}],
    }


def _resolved_document(root: Path) -> dict[str, object]:
    return {"services": {name: _service(root, name) for name in _WORKERS}}


def _policy_run(
    tmp_path: Path,
    command: str,
    *extra: str,
    config: dict[str, object] | None = None,
    document: dict[str, object] | None = None,
    rewrite_source: bool = True,
) -> subprocess.CompletedProcess[str]:
    config_root = tmp_path / "effective-config"
    config_root.mkdir(exist_ok=True)
    effective_config = config or {
        "unrelated": {"retained": True},
        "scheduled_ingestion": {
            "enabled": False,
            "manual_pause": True,
            "timezone": "Asia/Singapore",
            "local_opening": "09:00",
            "local_cutoff": "23:00",
            "drain_reserve_seconds": 900,
        },
    }
    if rewrite_source:
        (config_root / "ingestion-config.json").write_text(
            json.dumps(effective_config),
            encoding="utf-8",
        )
    document_path = tmp_path / "compose.json"
    document_path.write_text(
        json.dumps(document or _resolved_document(config_root)),
        encoding="utf-8",
    )
    prepare_arguments = ("--repository-root", str(tmp_path)) if command == "prepare" else ()
    return subprocess.run(
        [
            sys.executable,
            str(_POLICY_HELPER),
            command,
            "--compose-json",
            str(document_path),
            "--compose-directory",
            str(tmp_path),
            *prepare_arguments,
            *extra,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_policy_helper_preserves_complete_effective_config_without_copying_contents(
    tmp_path: Path,
) -> None:
    result = _policy_run(tmp_path, "prepare")

    assert result.returncode == 0, result.stderr
    source_path = tmp_path / "effective-config/ingestion-config.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    assert source["scheduled_ingestion"]["manual_pause"] is True
    assert "SCHEDULE_POLICY_ENABLED=false" in result.stdout
    assert "SCHEDULE_POLICY_NEEDS_MIGRATION=false" in result.stdout
    assert "EVIDENCE" not in result.stdout


def test_policy_helper_rejects_missing_effective_policy_key(tmp_path: Path) -> None:
    config = {
        "scheduled_ingestion": {
            "enabled": False,
            "timezone": "Asia/Singapore",
            "local_opening": "09:00",
            "local_cutoff": "23:00",
        }
    }

    result = _policy_run(tmp_path, "inspect", config=config)

    assert result.returncode == 2
    assert "drain_reserve_seconds is required" in result.stderr


def test_policy_probe_and_prepare_atomically_fill_only_missing_policy_keys(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    config = {
        "unrelated": {"retained": True},
        "scheduled_ingestion": {
            "enabled": False,
            "manual_pause": True,
            "timezone": "Asia/Singapore",
            "local_opening": "09:00",
        },
    }

    probe = _policy_run(tmp_path, "probe", config=config)
    source_path = tmp_path / "effective-config/ingestion-config.json"
    os.chmod(source_path, 0o600)
    source_metadata = source_path.stat()
    prior_umask = os.umask(0o000)
    try:
        prepared = _policy_run(
            tmp_path,
            "prepare",
            config=config,
            rewrite_source=False,
        )
    finally:
        os.umask(prior_umask)
    strict = _policy_run(tmp_path, "inspect", rewrite_source=False)
    payload = json.loads(source_path.read_text(encoding="utf-8"))

    assert probe.returncode == 0, probe.stderr
    assert "SCHEDULE_POLICY_NEEDS_MIGRATION=true" in probe.stdout
    assert prepared.returncode == 0, prepared.stderr
    assert strict.returncode == 0, strict.stderr
    assert payload["unrelated"] == {"retained": True}
    assert payload["scheduled_ingestion"]["enabled"] is False
    assert payload["scheduled_ingestion"]["manual_pause"] is True
    assert payload["scheduled_ingestion"]["local_cutoff"] == "23:00"
    assert payload["scheduled_ingestion"]["drain_reserve_seconds"] == 900
    prepared_metadata = source_path.stat()
    assert prepared_metadata.st_mode & 0o777 == 0o600
    assert prepared_metadata.st_uid == source_metadata.st_uid
    assert prepared_metadata.st_gid == source_metadata.st_gid


def test_policy_prepare_preserves_legacy_bare_exclusion_loader_semantics(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    bare_exclusions = {
        "emails": ["operator@example.test"],
        "email_domains": ["example.test"],
        "names": ["Operator"],
    }
    _policy_run(tmp_path, "probe", config=bare_exclusions)
    source_path = tmp_path / "effective-config/ingestion-config.json"
    before = load_ingestion_config(str(source_path)).exclusions

    prepared = _policy_run(
        tmp_path,
        "prepare",
        config=bare_exclusions,
        rewrite_source=False,
    )
    after = load_ingestion_config(str(source_path)).exclusions
    payload = json.loads(source_path.read_text(encoding="utf-8"))

    assert prepared.returncode == 0, prepared.stderr
    assert after == before
    assert payload["exclusions"] == bare_exclusions
    assert payload["scheduled_ingestion"]["timezone"] == "Asia/Singapore"


def test_atomic_prepare_rejects_fchown_failure_before_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "scheduled_ingestion_policy_test_module"
    specification = spec_from_file_location(module_name, _POLICY_HELPER)
    assert specification is not None and specification.loader is not None
    module = module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    source_path = tmp_path / "ingestion-config.json"
    source_path.write_text('{"scheduled_ingestion": {}}\n', encoding="utf-8")
    before = source_path.read_text(encoding="utf-8")

    def refuse_fchown(*_args: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(module.os, "fchown", refuse_fchown, raising=False)
    with pytest.raises(module.PolicyError, match="atomically prepare"):
        module._atomic_replace(source_path, {"scheduled_ingestion": {"enabled": False}})

    assert source_path.read_text(encoding="utf-8") == before


def test_atomic_prepare_creates_owner_only_temp_before_final_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_name = "scheduled_ingestion_policy_creation_mode_test_module"
    specification = spec_from_file_location(module_name, _POLICY_HELPER)
    assert specification is not None and specification.loader is not None
    module = module_from_spec(specification)
    sys.modules[module_name] = module
    specification.loader.exec_module(module)
    source_path = tmp_path / "ingestion-config.json"
    source_path.write_text('{"scheduled_ingestion": {}}\n', encoding="utf-8")
    os.chmod(source_path, 0o640)
    source_metadata = source_path.stat()
    observed_create_modes: list[int] = []
    real_open = module.os.open

    def observe_open(path: object, flags: int, mode: int = 0o777) -> int:
        if flags & module.os.O_CREAT:
            observed_create_modes.append(mode)
        return real_open(path, flags, mode)

    monkeypatch.setattr(module.os, "open", observe_open)
    prior_umask = os.umask(0o000)
    try:
        module._atomic_replace(source_path, {"scheduled_ingestion": {"enabled": False}})
    finally:
        os.umask(prior_umask)
    replaced_metadata = source_path.stat()

    assert observed_create_modes == [0o600]
    assert replaced_metadata.st_mode & 0o777 == 0o640
    assert replaced_metadata.st_uid == source_metadata.st_uid
    assert replaced_metadata.st_gid == source_metadata.st_gid


def test_policy_prepare_refuses_tracked_effective_config_before_write(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    config = {
        "scheduled_ingestion": {
            "enabled": False,
            "timezone": "Asia/Singapore",
        }
    }
    _policy_run(tmp_path, "probe", config=config)
    source_path = tmp_path / "effective-config/ingestion-config.json"
    before = source_path.read_text(encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", str(source_path.relative_to(tmp_path))],
        check=True,
    )

    result = _policy_run(tmp_path, "prepare", config=config)

    assert result.returncode == 2
    assert "tracked" in result.stderr
    assert source_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    ("now", "allowed"),
    [
        ("2026-09-17T08:59:59+08:00", False),
        ("2026-09-17T09:00:00+08:00", True),
        ("2026-09-17T22:44:59+08:00", True),
        ("2026-09-17T22:45:00+08:00", False),
        ("2026-09-17T23:00:00+08:00", False),
        ("2026-09-18T00:00:00+08:00", False),
    ],
)
def test_policy_clock_admission_has_exact_open_and_drain_boundaries(
    tmp_path: Path,
    now: str,
    allowed: bool,
) -> None:
    result = _policy_run(tmp_path, "admit", "--now", now)

    assert (result.returncode == 0) is allowed


def test_policy_helper_rejects_worker_disagreement_and_out_of_policy_values(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    (config_root / "ingestion-config.json").write_text(
        json.dumps(
            {
                "scheduled_ingestion": {
                    "enabled": False,
                    "timezone": "UTC",
                    "local_opening": "09:00",
                    "local_cutoff": "23:00",
                    "drain_reserve_seconds": 900,
                }
            }
        ),
        encoding="utf-8",
    )
    document = _resolved_document(config_root)
    services = document["services"]
    assert isinstance(services, dict)
    beat = services["beat"]
    assert isinstance(beat, dict)
    environment = beat["environment"]
    assert isinstance(environment, dict)
    environment["INGESTION_CONFIG_FILE"] = "/app/config/other.json"
    document_path = tmp_path / "compose.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_POLICY_HELPER), "inspect", "--compose-json", str(document_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "policy error" in result.stderr


def test_policy_probe_rejects_explicit_conflicting_policy_value(tmp_path: Path) -> None:
    config = {
        "scheduled_ingestion": {
            "enabled": False,
            "timezone": "UTC",
            "local_opening": "09:00",
            "local_cutoff": "23:00",
            "drain_reserve_seconds": 900,
        }
    }

    result = _policy_run(tmp_path, "probe", config=config)

    assert result.returncode == 2
    assert "approved deployment window" in result.stderr


@pytest.mark.parametrize(
    ("service_name", "field", "value"),
    [
        ("ingestion-worker", "cpus", "1.1"),
        ("lifecycle-worker", "mem_limit", "2G"),
        ("beat", "stop_grace_period", "15m0s"),
        ("ingestion-worker", "command", ["celery", "worker", "--concurrency=2"]),
        ("lifecycle-worker", "command", ["celery", "worker", "--concurrency=0"]),
    ],
)
def test_policy_helper_rejects_resource_grace_and_concurrency_ceiling_violations(
    tmp_path: Path,
    service_name: str,
    field: str,
    value: object,
) -> None:
    config_root = tmp_path / "effective-config"
    config_root.mkdir()
    config = {
        "scheduled_ingestion": {
            "enabled": False,
            "timezone": "Asia/Singapore",
            "local_opening": "09:00",
            "local_cutoff": "23:00",
            "drain_reserve_seconds": 900,
        }
    }
    (config_root / "ingestion-config.json").write_text(json.dumps(config), encoding="utf-8")
    document = _resolved_document(config_root)
    services = document["services"]
    assert isinstance(services, dict)
    service = services[service_name]
    assert isinstance(service, dict)
    service[field] = value
    document_path = tmp_path / "compose.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_POLICY_HELPER), "inspect", "--compose-json", str(document_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2


def test_policy_helper_accepts_compound_stop_grace_below_reserve(tmp_path: Path) -> None:
    config_root = tmp_path / "effective-config"
    config_root.mkdir()
    (config_root / "ingestion-config.json").write_text(
        json.dumps(
            {
                "scheduled_ingestion": {
                    "enabled": False,
                    "timezone": "Asia/Singapore",
                    "local_opening": "09:00",
                    "local_cutoff": "23:00",
                    "drain_reserve_seconds": 900,
                }
            }
        ),
        encoding="utf-8",
    )
    document = _resolved_document(config_root)
    services = document["services"]
    assert isinstance(services, dict)
    worker = services["ingestion-worker"]
    assert isinstance(worker, dict)
    worker["stop_grace_period"] = "5m0s"
    document_path = tmp_path / "compose.json"
    document_path.write_text(json.dumps(document), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_POLICY_HELPER), "inspect", "--compose-json", str(document_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_legacy_pause_marker_migrates_only_from_an_otherwise_clean_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "staging"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / ".gitignore").write_text(".docker/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore state"], check=True)
    (repo / ".lifecycle-worker-paused").touch()

    result = subprocess.run(
        ["bash", str(_CONTROL), "migrate-legacy"],
        cwd=repo,
        env={**os.environ, "HYPERP_DEPLOY_LOCK_FILE": str(tmp_path / "lock")},
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (repo / ".lifecycle-worker-paused").exists()
    assert (repo / ".docker/staging/data/worker-pauses/lifecycle-worker").is_file()


@pytest.mark.parametrize(
    "paused",
    [
        ",".join(selection)
        for size in range(len(_WORKERS) + 1)
        for selection in combinations(_WORKERS, size)
    ],
)
def test_guard_separates_running_and_stopped_recreation_for_all_pause_combinations(
    paused: str,
) -> None:
    result = subprocess.run(
        [str(_GUARD), "plan", paused, "api", *_WORKERS],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "RUNNING_RECREATE_SERVICES=" in result.stdout
    assert "STOPPED_RECREATE_SERVICES=" in result.stdout
    running = next(line for line in result.stdout.splitlines() if line.startswith("RUNNING_"))
    stopped = next(line for line in result.stdout.splitlines() if line.startswith("STOPPED_"))
    for service in _WORKERS:
        is_paused = service in paused.split(",")
        assert (service in stopped) is is_paused
        assert (service in running) is not is_paused


def test_intent_output_drives_empty_pause_csv_to_running_guard_plan(tmp_path: Path) -> None:
    repo = tmp_path / "staging"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = subprocess.run(
        ["bash", str(_CONTROL), "intent"],
        cwd=repo,
        env={**os.environ, "HYPERP_DEPLOY_LOCK_FILE": str(tmp_path / "lock")},
        check=False,
        capture_output=True,
        text=True,
    )
    guard = subprocess.run(
        [str(_GUARD), "plan", "", *_WORKERS],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "INGESTION_WORKER_PAUSED=false" in result.stdout
    assert "LIFECYCLE_WORKER_PAUSED=false" in result.stdout
    assert "BEAT_PAUSED=false" in result.stdout
    assert "PAUSED=FALSE" not in result.stdout
    assert guard.returncode == 0
    assert "STOPPED_RECREATE_SERVICES=''" in guard.stdout


def test_intent_output_drives_real_marker_to_stopped_guard_plan(tmp_path: Path) -> None:
    repo = tmp_path / "staging"
    marker = repo / ".docker/staging/data/worker-pauses/ingestion-worker"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    marker.parent.mkdir(parents=True)
    marker.touch()
    result = subprocess.run(
        ["bash", str(_CONTROL), "intent"],
        cwd=repo,
        env={**os.environ, "HYPERP_DEPLOY_LOCK_FILE": str(tmp_path / "lock")},
        check=False,
        capture_output=True,
        text=True,
    )
    guard = subprocess.run(
        [str(_GUARD), "plan", "ingestion-worker", *_WORKERS],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "INGESTION_WORKER_PAUSED=true" in result.stdout
    assert "INGESTION_WORKER_PAUSED=TRUE" not in result.stdout
    assert guard.returncode == 0
    assert "STOPPED_RECREATE_SERVICES=ingestion-worker" in guard.stdout


def test_dormant_intelligence_is_profiled_and_deploy_neutralizes_ambient_profiles() -> None:
    deployment = _DEPLOY.read_text(encoding="utf-8")
    for compose in _COMPOSES:
        content = compose.read_text(encoding="utf-8")
        intelligence = content.split("  intelligence:", 1)[1].split("  neo4j:", 1)[0]
        assert 'profiles: ["intelligence"]' in intelligence
        assert "intelligence-data:/var/lib/intelligence" in intelligence
        assert (
            "INTELLIGENCE_MUTATIONS_ENABLED: ${INTELLIGENCE_MUTATIONS_ENABLED:-false}"
            in intelligence
        )
        assert "INTELLIGENCE_CRM_ACTIVITY_CLEANUP_ENABLED:" in intelligence
    assert "COMPOSE=(env COMPOSE_PROFILES= docker compose" in deployment
    assert "intelligence" not in deployment.split("EXPECTED_SERVICES=", 1)[1].split(")", 1)[0]
    assert "up --no-start --no-deps --force-recreate" in deployment
    assert "lacks required --no-start support" in deployment
    assert "lacks required --no-deps support" in deployment
    assert "stop intelligence" not in deployment
    assert "rm -f intelligence" not in deployment


def test_deployment_retains_active_publication_fencing_and_activity_retirement_guards() -> None:
    scheduled = (_ROOT / "services/ingestion/tests/test_scheduled_ingestion_tasks.py").read_text(
        encoding="utf-8"
    )
    fencing = (_ROOT / "services/ingestion/tests/test_ingestion_control_queries.py").read_text(
        encoding="utf-8"
    )
    assert "disabled dispatch must not resolve a group" in scheduled
    assert "active successor must not publish legacy Bitrix" in scheduled
    assert "logical.active_generation = $generation" in fencing
    assert "stop_requested_at IS NULL" in fencing


def test_deployment_captures_helper_output_before_eval_and_fails_closed() -> None:
    deployment = _DEPLOY.read_text(encoding="utf-8")
    assert 'if ! worker_intent="$(' in deployment
    assert 'if ! policy_probe="$(python3 "${POLICY_HELPER}" probe' in deployment
    assert 'if ! policy_prepare="$(python3 "${POLICY_HELPER}" prepare' in deployment
    assert 'if ! policy_inspect="$(python3 "${POLICY_HELPER}" inspect' in deployment
    assert 'eval "${worker_intent}"' in deployment
    assert 'eval "${policy_probe}"' in deployment
    assert 'eval "${policy_prepare}"' in deployment
    assert 'eval "${policy_inspect}"' in deployment
    assert "python3 is not installed" in deployment


def test_deployment_prepares_and_strictly_reads_back_policy_without_worker_recreation() -> None:
    deployment = _DEPLOY.read_text(encoding="utf-8")
    initial_gate = deployment.index('if [[ "${SCHEDULE_POLICY_ENABLED:-false}" == true')
    prepare = deployment.index('if ! policy_prepare="$(python3 "${POLICY_HELPER}" prepare')
    strict_readback = deployment.index('if ! policy_inspect="$(python3 "${POLICY_HELPER}" inspect')
    build = deployment.index('CURRENT_PHASE="building and recreating changed services"')

    assert initial_gate < prepare < strict_readback < build


def test_first_deploy_defers_new_helper_requirements_until_after_fast_forward() -> None:
    deployment = _DEPLOY.read_text(encoding="utf-8")
    merge = deployment.index('git -C "${REPO_DIR}" merge --ff-only "${EXPECTED_SHA}"')
    before_merge = deployment[:merge]
    after_merge = deployment[merge:]

    assert "assert_pre_merge_checkout_state" in before_merge
    assert "scripts/worker-control.sh" not in before_merge
    assert "scheduled_ingestion_policy.py" not in before_merge
    assert '"?? .lifecycle-worker-paused"' in before_merge
    assert "scripts/worker-control.sh" in after_merge
    assert "scheduled_ingestion_policy.py" in after_merge
    assert '"${WORKER_CONTROL}" migrate-legacy' in after_merge
    assert after_merge.index('"${WORKER_CONTROL}" migrate-legacy') < after_merge.index(
        "assert_git_sync"
    )


def _deploy_function(script: str, name: str) -> str:
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start) + len("\n}\n")
    return script[start:end]


@pytest.mark.parametrize(
    ("legacy_contents", "extra_dirty_file", "expected_returncode"),
    [
        (None, None, 0),
        ("", None, 0),
        ("not-empty", None, 1),
        ("", "unrelated.txt", 1),
    ],
)
def test_pre_merge_fixture_allows_only_clean_or_safe_legacy_marker(
    tmp_path: Path,
    legacy_contents: str | None,
    extra_dirty_file: str | None,
    expected_returncode: int,
) -> None:
    repo = tmp_path / "staging"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    if legacy_contents is not None:
        (repo / ".lifecycle-worker-paused").write_text(legacy_contents, encoding="utf-8")
    if extra_dirty_file is not None:
        (repo / extra_dirty_file).write_text("unexpected", encoding="utf-8")
    deploy = _DEPLOY.read_text(encoding="utf-8")
    function = _deploy_function(deploy, "assert_pre_merge_checkout_state")
    command = "\n".join(
        (
            "set -euo pipefail",
            "fail() { exit 1; }",
            function,
            f"REPO_DIR={shlex.quote(str(repo))}",
            "assert_pre_merge_checkout_state",
        )
    )

    result = subprocess.run(["bash", "-c", command], check=False, capture_output=True, text=True)

    assert result.returncode == expected_returncode


def test_deployment_enforces_each_marked_worker_stopped_before_planning() -> None:
    deployment = _DEPLOY.read_text(encoding="utf-8")
    intent = deployment.index('"${WORKER_CONTROL}" intent')
    enforce = deployment.index('"${WORKER_CONTROL}" enforce "${worker_service}"')
    planning = deployment.index('CURRENT_PHASE="planning selective rebuild"')

    assert intent < enforce < planning
