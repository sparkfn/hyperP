"""Auth domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

Role = Literal["admin", "employee", "first_time"]


class AuthUser(BaseModel):
    """Represents an authenticated principal resolved from a Google ID token."""

    email: str
    google_sub: str
    role: Role
    entity_key: str | None = None
    display_name: str | None = None
