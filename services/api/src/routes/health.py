"""Health check endpoint."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.graph.client import verify_connectivity

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    neo4j: str
    timestamp: str
    error: str | None = None


@router.get("/health", response_model=HealthResponse)
async def health() -> JSONResponse:
    """Report Neo4j connectivity."""
    try:
        await verify_connectivity()
        body = HealthResponse(
            status="ok", neo4j="connected", timestamp=datetime.now(UTC).isoformat()
        )
        return JSONResponse(body.model_dump(), status_code=200)
    except Exception as exc:  # noqa: BLE001 — surface message for ops
        body = HealthResponse(
            status="degraded",
            neo4j="disconnected",
            timestamp=datetime.now(UTC).isoformat(),
            error=str(exc),
        )
        return JSONResponse(body.model_dump(), status_code=503)
