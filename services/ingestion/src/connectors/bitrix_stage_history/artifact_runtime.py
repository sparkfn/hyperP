"""Configuration-backed construction for restricted Bitrix artifact storage."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.connectors.bitrix_stage_history.artifact_signing import (
    StaticArtifactSigningKeyProvider,
)
from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore

if TYPE_CHECKING:
    from src.config import Settings


@dataclass(frozen=True)
class ArtifactStoreConfiguration:
    """Non-secret artifact-store locations plus an externally supplied signing key."""

    primary_root: Path
    backup_root: Path
    signing_key_id: str
    signing_key_secret: bytes = field(repr=False)
    retained_verification_keys: Mapping[str, bytes] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.primary_root == self.backup_root:
            raise ValueError("artifact primary and backup roots must differ")
        if not self.signing_key_id.strip():
            raise ValueError("artifact signing key ID must be non-empty")
        if len(self.signing_key_secret) < 32:
            raise ValueError("artifact signing key secret must contain at least 32 bytes")
        if self.signing_key_id in self.retained_verification_keys:
            raise ValueError("active artifact signing key cannot also be retained")
        for key_id, secret in self.retained_verification_keys.items():
            if not key_id.strip() or len(secret) < 32:
                raise ValueError("retained artifact verification keys are invalid")

    def open(self) -> LocalRestrictedArtifactStore:
        keys = dict(self.retained_verification_keys)
        keys[self.signing_key_id] = self.signing_key_secret
        provider = StaticArtifactSigningKeyProvider(self.signing_key_id, keys)
        return LocalRestrictedArtifactStore(
            self.primary_root,
            self.backup_root,
            provider,
        )


def decode_signing_secret(value: str) -> bytes:
    """Decode a secret supplied as ``hex:<hex>`` or an opaque UTF-8 value."""
    if value.startswith("hex:"):
        try:
            decoded = bytes.fromhex(value.removeprefix("hex:"))
        except ValueError as exc:
            raise ValueError("artifact signing key hex is invalid") from exc
    else:
        decoded = value.encode("utf-8")
    if len(decoded) < 32:
        raise ValueError("artifact signing key secret must contain at least 32 bytes")
    return decoded


def sha256_digest(value: bytes | str) -> str:
    encoded = value if isinstance(value, bytes) else value.encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def retained_keys_from_environment(specifications: list[str]) -> dict[str, bytes]:
    """Resolve repeatable ``KEY_ID=ENV_NAME`` retained-key specifications."""
    resolved: dict[str, bytes] = {}
    for specification in specifications:
        key_id, separator, environment_name = specification.partition("=")
        if not separator or not key_id.strip() or not environment_name.strip():
            raise ValueError("retained artifact keys must use KEY_ID=ENV_NAME")
        if key_id in resolved:
            raise ValueError("retained artifact key IDs must be unique")
        raw_secret = os.environ.get(environment_name)
        if raw_secret is None:
            raise ValueError(f"retained artifact key environment is missing: {environment_name}")
        resolved[key_id] = decode_signing_secret(raw_secret)
    return resolved


def stage_history_store_from_settings(settings: Settings) -> LocalRestrictedArtifactStore:
    """Open the restricted store without exposing its signing secret."""
    secret = decode_signing_secret(
        settings.stage_history_artifact_signing_key_secret.get_secret_value()
    )
    return ArtifactStoreConfiguration(
        primary_root=Path(settings.stage_history_artifact_primary_root),
        backup_root=Path(settings.stage_history_artifact_backup_root),
        signing_key_id=settings.stage_history_artifact_signing_key_id,
        signing_key_secret=secret,
    ).open()
