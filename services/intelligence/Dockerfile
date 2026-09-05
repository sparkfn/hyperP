FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"
COPY --from=astral/uv:0.10 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/api/pyproject.toml services/api/pyproject.toml
COPY services/ingestion/pyproject.toml services/ingestion/pyproject.toml
COPY services/intelligence/pyproject.toml services/intelligence/pyproject.toml
COPY services/intelligence/src/ services/intelligence/src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package hyperp-intelligence \
    && groupadd --gid 1000 intelligence \
    && useradd --uid 1000 --gid 1000 --no-create-home intelligence \
    && mkdir -p /var/lib/intelligence \
    && chown intelligence:intelligence /var/lib/intelligence
USER intelligence
WORKDIR /app/services/intelligence
CMD ["intelligence", "idle"]
