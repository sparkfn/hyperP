"""JSON-backed ingestion exclusion configuration."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from src.models import JsonValue


@dataclass
class ExclusionFile:
    """Editable hard-exclusion values loaded from JSON."""

    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    email_domains: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    source_ids: list[str] = field(default_factory=list)


def _str_list(raw: JsonValue, *, path: Path) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    values: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
        values.append(value)
    return values


def load_exclusion_file(path_value: str) -> ExclusionFile:
    if not path_value.strip():
        return ExclusionFile()
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Ingestion exclusions file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion exclusions JSON: {path}")
    payload = cast(dict[str, JsonValue], raw)
    return ExclusionFile(
        phones=_str_list(payload.get("phones"), path=path),
        emails=_str_list(payload.get("emails"), path=path),
        email_domains=_str_list(payload.get("email_domains"), path=path),
        names=_str_list(payload.get("names"), path=path),
        source_ids=_str_list(payload.get("source_ids"), path=path),
    )
