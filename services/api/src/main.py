"""Uvicorn entrypoint."""

from __future__ import annotations

import uvicorn

from src.config import config


def main() -> None:
    """Run the FastAPI app under uvicorn."""
    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",  # noqa: S104 — container service binds to all interfaces
        port=config.port,
        log_level=config.log_level.lower(),
    )


if __name__ == "__main__":
    main()
