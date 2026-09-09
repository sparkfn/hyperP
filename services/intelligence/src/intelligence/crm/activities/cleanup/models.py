"""Strict immutable CLI request values for activity cleanup admission."""

from __future__ import annotations

from dataclasses import dataclass

from intelligence.crm.activities.models import validate_snapshot_id


def _text(value: str, field: str) -> None:
    if not value or len(value) > 256 or any(character in value for character in "/\\\x00"):
        raise ValueError(f"{field} is invalid")


def _digest(value: str, field: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} is invalid")


@dataclass(frozen=True)
class CleanupAuthorization:
    checkpoint_id: str
    accepted_run_id: str
    snapshot_id: str
    manifest_digest: str

    def __post_init__(self) -> None:
        validate_snapshot_id(self.checkpoint_id)
        _text(self.accepted_run_id, "accepted_run_id")
        validate_snapshot_id(self.snapshot_id)
        _digest(self.manifest_digest, "manifest_digest")

    def as_dict(self) -> dict[str, object]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "accepted_run_id": self.accepted_run_id,
            "snapshot_id": self.snapshot_id,
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True)
class CleanupTarget:
    environment_id: str
    database_identity: str

    def __post_init__(self) -> None:
        _text(self.environment_id, "environment_id")
        _text(self.database_identity, "database_identity")

    def as_dict(self) -> dict[str, object]:
        return {"environment_id": self.environment_id, "database_identity": self.database_identity}


@dataclass(frozen=True)
class CleanupRequest:
    authorization: CleanupAuthorization
    target: CleanupTarget
    batch_size: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.batch_size, int)
            or isinstance(self.batch_size, bool)
            or not 1 <= self.batch_size <= 1_000
        ):
            raise ValueError("cleanup batch_size is outside approved bounds")

    def as_dict(self) -> dict[str, object]:
        return {
            "authorization": self.authorization.as_dict(),
            "target": self.target.as_dict(),
            "batch_size": self.batch_size,
        }
