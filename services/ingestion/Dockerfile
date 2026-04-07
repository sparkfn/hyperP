FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Install uv from the official distroless image (no curl/network deps).
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first for layer caching. Build context is the repo
# root (see docker-compose.yml) so the canonical init.cypher is reachable.
COPY services/ingestion/pyproject.toml services/ingestion/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Bake the Neo4j init script into the image so every ingestion run can
# re-apply schema (idempotent — see src/graph/schema_init.py).
COPY infra/neo4j/init.cypher ./infra/neo4j/init.cypher

# Now copy the source and install the project itself.
COPY services/ingestion/src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home appuser
USER appuser

CMD ["python", "-m", "src.main", "--source-key", "fundbox", "--mode", "batch"]
