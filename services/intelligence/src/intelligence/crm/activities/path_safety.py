"""Lexical, link-free filesystem admission for private checkpoint paths."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def has_link_or_reparse(metadata: os.stat_result) -> bool:
    """Return whether lstat metadata denotes a link or Windows reparse point."""
    raw_attributes = getattr(metadata, "st_file_attributes", 0)
    attributes = raw_attributes if isinstance(raw_attributes, int) else 0
    raw_reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    reparse = raw_reparse if isinstance(raw_reparse, int) else 0
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse)


def confined_directory(workspace: Path, parts: tuple[str, ...], create: bool) -> Path:
    """Admit a workspace-relative directory after lstat-checking every ancestor.

    Components are deliberately lexical: this function never resolves a path and
    therefore never follows a symlink or reparse-like ancestor while constructing
    the result.
    """
    _component_parts(parts)
    _require_directory(workspace, "workspace")
    current = workspace
    for component in parts:
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if not create:
                raise ValueError("checkpoint path is missing") from None
            current.mkdir(mode=0o700)
            metadata = current.lstat()
        if has_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("checkpoint path is unsafe")
    return current


def confined_file(workspace: Path, parts: tuple[str, ...]) -> Path:
    """Return a leaf below admitted workspace-relative directory components."""
    if len(parts) < 2:
        raise ValueError("checkpoint file path is incomplete")
    _component_parts(parts)
    directory = confined_directory(workspace, parts[:-1], create=False)
    return directory / parts[-1]


def _require_directory(path: Path, field: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        raise ValueError(f"{field} is missing") from None
    if has_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"{field} is unsafe")


def _component_parts(parts: tuple[str, ...]) -> None:
    for part in parts:
        if not part or part in {".", ".."} or Path(part).name != part:
            raise ValueError("checkpoint path component is unsafe")
