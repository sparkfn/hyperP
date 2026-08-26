"""Strict versioned cursors for machine identity-link synchronization."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class IdentityLinkCursor:
    kind: Literal["events", "snapshot"]
    after_revision: int | None = None
    through_revision: int | None = None
    snapshot_revision: int | None = None
    after_link_key: str | None = None


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"invalid cursor {name}")
    return value


def encode_identity_link_cursor(cursor: IdentityLinkCursor) -> str:
    """Encode a private transport cursor."""
    payload: dict[str, object] = {"cursor_version": 1, "kind": cursor.kind}
    if cursor.kind == "events":
        if cursor.after_revision is None or cursor.through_revision is None:
            raise ValueError("event cursor requires revision bounds")
        if cursor.after_revision < 0 or cursor.after_revision >= cursor.through_revision:
            raise ValueError("event cursor must advance within its frozen bound")
        payload.update(
            after_revision=cursor.after_revision,
            through_revision=cursor.through_revision,
        )
    else:
        if cursor.snapshot_revision is None or cursor.after_link_key is None:
            raise ValueError("snapshot cursor requires snapshot bound and key")
        payload.update(
            snapshot_revision=cursor.snapshot_revision,
            after_link_key=cursor.after_link_key,
        )
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def decode_identity_link_cursor(
    value: str, expected_kind: Literal["events", "snapshot"]
) -> IdentityLinkCursor:
    """Decode and strictly validate a private transport cursor."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid cursor") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("cursor_version") != 1
        or payload.get("kind") != expected_kind
    ):
        raise ValueError("invalid cursor")
    if expected_kind == "events":
        after = _integer(payload.get("after_revision"), "after_revision")
        through = _integer(payload.get("through_revision"), "through_revision")
        if after >= through:
            raise ValueError("invalid cursor")
        return IdentityLinkCursor(kind="events", after_revision=after, through_revision=through)
    revision = _integer(payload.get("snapshot_revision"), "snapshot_revision")
    key = payload.get("after_link_key")
    if not isinstance(key, str) or not key:
        raise ValueError("invalid cursor")
    return IdentityLinkCursor(kind="snapshot", snapshot_revision=revision, after_link_key=key)
