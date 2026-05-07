"""Dump-file discovery endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request

from src.auth.deps import require_scope
from src.config import config
from src.http_utils import envelope, http_error
from src.types import ApiResponse

router = APIRouter()


def list_dump_files(root: Path) -> list[str]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise NotADirectoryError(str(root))
    files: list[str] = []
    for path in resolved.rglob("*"):
        candidate = path.resolve(strict=True)
        if not candidate.is_relative_to(resolved) or not candidate.is_file():
            continue
        files.append(path.relative_to(resolved).as_posix())
    return sorted(files)


@router.get(
    "/v1/dumps",
    response_model=ApiResponse[list[str]],
    dependencies=[Depends(require_scope("ingest:write"))],
)
async def list_dumps(request: Request) -> ApiResponse[list[str]]:
    try:
        files = list_dump_files(Path(config.dumps_root))
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        raise http_error(
            503,
            "dumps_root_unavailable",
            "Dumps directory is not mounted or readable.",
            request,
        ) from None
    return envelope(files, request)
