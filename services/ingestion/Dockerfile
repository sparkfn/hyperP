FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Install uv from the official distroless image (no curl/network deps).
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app

# uv workspace layout: lockfile lives at the repo root and resolves all
# members. Build context must be the repo root (see docker-compose.yml).
COPY pyproject.toml uv.lock ./
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml

# Install only the ingestion member and its transitive deps. Members are
# virtual (package = false), so this only materialises third-party deps.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package profile-unifier-ingestion

# Bake the Neo4j init script into the image so every ingestion run can
# re-apply schema (idempotent — see src/graph/schema_init.py).
COPY infra/neo4j/init.cypher ./infra/neo4j/init.cypher

COPY services/ingestion/src/ services/ingestion/src/

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home appuser
USER appuser

WORKDIR /app/services/ingestion

CMD ["python", "-m", "src.main", "--source-key", "fundbox", "--mode", "batch"]
