FROM python:3.12-slim AS base

WORKDIR /app

# Install dependencies first for layer caching
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

# Copy application source
COPY src/ ./src/

# Create non-root user
RUN groupadd --gid 1000 appuser \
    && useradd --uid 1000 --gid 1000 --no-create-home appuser
USER appuser

CMD ["python", "-m", "src.main", "--source-key", "default", "--mode", "batch"]
