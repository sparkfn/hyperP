"""Externally supplied signing keys for restricted artifact manifests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ArtifactSigningKey:
    """One externally managed manifest-authentication key."""

    key_id: str
    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.key_id:
            raise ValueError("artifact signing key ID must be non-empty")
        if len(self.secret) < 32:
            raise ValueError("artifact signing key must contain at least 32 bytes")


class ArtifactSigningKeyProvider(Protocol):
    """Resolve the active key and retained verification keys without storing them."""

    def current(self) -> ArtifactSigningKey: ...

    def get(self, key_id: str) -> ArtifactSigningKey | None: ...


class StaticArtifactSigningKeyProvider:
    """Typed provider suitable for configuration-backed key injection and rotation."""

    def __init__(self, current_key_id: str, keys: Mapping[str, bytes]) -> None:
        if current_key_id not in keys:
            raise ValueError("current artifact signing key ID is unavailable")
        self._current_key_id = current_key_id
        self._keys = {
            key_id: ArtifactSigningKey(key_id=key_id, secret=secret)
            for key_id, secret in keys.items()
        }

    def current(self) -> ArtifactSigningKey:
        return self._keys[self._current_key_id]

    def get(self, key_id: str) -> ArtifactSigningKey | None:
        return self._keys.get(key_id)
