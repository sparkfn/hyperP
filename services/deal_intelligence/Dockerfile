FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH

# Install uv from the official distroless image (no curl/network deps).
COPY --from=astral/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app

# The workspace lockfile resolves every member, so retain each member manifest
# before the frozen production install. The build context is the repository root.
COPY pyproject.toml uv.lock ./
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml
COPY services/deal_intelligence/pyproject.toml services/deal_intelligence/pyproject.toml
COPY services/deal_intelligence/src/ services/deal_intelligence/src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package profile-unifier-deal-intelligence

RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home appuser
USER appuser

WORKDIR /app/services/deal_intelligence

# Compose selects the explicit API, worker, scheduler, or migration command.
CMD ["deal-intelligence-api"]
