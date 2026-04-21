"""Authentication endpoints: exchange Google ID token for principal info."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from src.auth.deps import get_current_user
from src.auth.models import AuthUser
from src.http_utils import envelope
from src.types import ApiResponse

router = APIRouter(prefix="/v1/auth")


class MeResponse(BaseModel):
    email: str
    google_sub: str
    role: str
    entity_key: str | None = None
    display_name: str | None = None


def _to_response(user: AuthUser) -> MeResponse:
    return MeResponse(
        email=user.email,
        google_sub=user.google_sub,
        role=user.role,
        entity_key=user.entity_key,
        display_name=user.display_name,
    )


@router.get("/me", response_model=ApiResponse[MeResponse])
async def read_me(
    request: Request, user: AuthUser = Depends(get_current_user)
) -> ApiResponse[MeResponse]:
    """Return the authenticated user's role and entity assignment."""
    return envelope(_to_response(user), request)
