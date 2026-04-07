"""Apply Neo4j constraints + indexes from the canonical init.cypher script.

This is the durable fix for the missing-index slowdown: the ingestion service
applies the schema on every run instead of relying on an out-of-band step
against the Neo4j container. The init script is fully idempotent (every
statement uses ``IF NOT EXISTS``), so calling this on every startup is safe
and free when the schema is already in place.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.graph.client import Neo4jClient

logger = logging.getLogger(__name__)

# init.cypher is copied into the image at /app/infra/neo4j/init.cypher by the
# Dockerfile. In local development we fall back to the repo path so the same
# code works under `uv run`.
def _candidate_paths() -> tuple[Path, ...]:
    """Return existing init.cypher candidates in priority order.

    Built lazily so a missing repo-root parent (e.g. in the container, where
    the source tree is rooted at /app) doesn't blow up at import time with
    an IndexError from ``parents[N]``.
    """
    here = Path(__file__).resolve()
    candidates: list[Path] = [
        # Production image path (Dockerfile copies it here).
        Path("/app/infra/neo4j/init.cypher"),
    ]
    # Walk up; the repo layout puts init.cypher 4 parents up, but in the
    # container src/ is at /app and that path doesn't exist. Try every
    # plausible ancestor instead of hard-coding an index.
    for parent in here.parents:
        candidates.append(parent / "infra" / "neo4j" / "init.cypher")
    return tuple(candidates)


def _find_init_cypher() -> Path:
    candidates = _candidate_paths()
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "init.cypher not found in any of: "
        + ", ".join(str(p) for p in candidates)
    )


def _split_statements(script: str) -> list[str]:
    """Split a multi-statement Cypher script on semicolons.

    Strips ``//`` line comments first; the script is hand-maintained so a
    naive splitter is sufficient (no string literals contain semicolons).
    """
    lines = []
    for raw in script.splitlines():
        stripped = raw.split("//", 1)[0].rstrip()
        if stripped:
            lines.append(stripped)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def apply_schema(client: Neo4jClient) -> int:
    """Apply every statement in init.cypher to the connected Neo4j instance.

    Returns the number of statements executed. Each statement runs in its own
    auto-commit transaction because Neo4j requires schema changes to be
    isolated from data changes.
    """
    path = _find_init_cypher()
    statements = _split_statements(path.read_text(encoding="utf-8"))
    logger.info("Applying %d schema statements from %s", len(statements), path)

    with client.session() as session:
        for stmt in statements:
            session.run(stmt).consume()
    logger.info("Schema applied (%d statements, idempotent)", len(statements))
    return len(statements)
