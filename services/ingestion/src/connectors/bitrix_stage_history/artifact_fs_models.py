"""Typed descriptor ownership models for restricted artifact storage."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArtifactStorageLimits:
    max_files: int = 64
    max_file_bytes: int = 2 * 1024 * 1024 * 1024
    max_total_bytes: int = 8 * 1024 * 1024 * 1024
    max_manifest_bytes: int = 1024 * 1024
    max_marker_bytes: int = 4096

    def __post_init__(self) -> None:
        values = (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_manifest_bytes,
            self.max_marker_bytes,
        )
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("artifact storage limits must be positive integers")


@dataclass
class SessionDirectory:
    artifact_id: str
    path: Path
    descriptor: int
    device: int
    inode: int

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)


@dataclass
class PreparedObject:
    artifact_id: str
    path: Path
    descriptor: int
    parent_descriptor: int
    device: int
    inode: int

    def close(self) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        if descriptor >= 0:
            os.close(descriptor)


@dataclass(frozen=True)
class PublishedObjectIdentity:
    path: Path
    device: int
    inode: int
    owner_uid: int
    group_gid: int
    mode: int


@dataclass(frozen=True)
class PublishedMarker:
    device: int
    inode: int
