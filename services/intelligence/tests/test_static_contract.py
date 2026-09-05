"""Static package, Docker, and Compose contract tests for Intelligence."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_package_and_docker_are_cli_only() -> None:
    project = (ROOT / "services" / "intelligence" / "pyproject.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "services" / "intelligence" / "Dockerfile").read_text(encoding="utf-8")
    assert 'intelligence = "intelligence.cli:main"' in project
    assert "USER intelligence" in dockerfile
    assert 'CMD ["intelligence", "idle"]' in dockerfile
    assert "EXPOSE" not in dockerfile


def test_compose_has_only_one_isolated_intelligence_service_and_volume() -> None:
    for path in (ROOT / "docker-compose.yml", ROOT / ".docker/staging/docker-compose.yml"):
        content = path.read_text(encoding="utf-8")
        service = content.split("  intelligence:\n", maxsplit=1)[1].split("\n  neo4j:", maxsplit=1)[
            0
        ]
        assert "- intelligence-data:/var/lib/intelligence" in service
        assert 'test: ["CMD", "intelligence", "health"]' in service
        assert "ports:" not in service
        assert "depends_on:" not in service
        assert "read_only: true" in service
        assert "cpus: ${INTELLIGENCE_CPUS:-0.5}" in service
        assert "mem_limit: ${INTELLIGENCE_MEMORY_LIMIT:-512M}" in service
        assert "pids_limit: ${INTELLIGENCE_PIDS_LIMIT:-128}" in service
        assert "init: true" in service
        assert "stop_grace_period: ${INTELLIGENCE_STOP_GRACE_PERIOD:-30s}" in service
        assert "cap_drop:" in service
        assert "no-new-privileges:true" in service
        assert "tmpfs:" in service
        assert "/tmp:rw,noexec,nosuid,size=64m" in service
        assert content.rstrip().endswith("volumes:\n  intelligence-data:")
