FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Install uv from the official distroless image (no curl/network deps).
COPY --from=astral/uv:0.10 /uv /usr/local/bin/uv

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

# --- Training target: adds numpy for model training (#125.4) ---------------
# Default target (base) does NOT include the training group. This target is
# used only for one-off training runs, not for the production worker image.
# Build with: docker build --target training -t stg-hyperp-training .
FROM base AS training
USER root
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --project /app --frozen --no-dev --group training \
        --package profile-unifier-ingestion
USER appuser
ENV OPENBLAS_NUM_THREADS=1 \
    OMP_NUM_THREADS=1

# Keep the normal worker image as the Dockerfile default. Without this final
# stage, Docker builds the preceding training stage when Compose omits target,
# pulling training-only dependencies into every worker and beat image.
FROM base AS production
