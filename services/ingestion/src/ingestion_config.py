"""Consolidated JSON config for ingestion: hard exclusions + LLM call tuning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from src.config import get_settings
from src.exclusion_config import (
    ExclusionFile,
    _machine_unit_identifier_list,
    _str_list,
)
from src.models import JsonValue


@dataclass
class LlmConfig:
    """LLM call tuning loaded from the consolidated config file."""

    timeout_seconds: float = 90.0
    request_delay_seconds: float = 0.5
    max_retries: int = 6
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 30.0
    # Chat extraction packs conversations into one LLM call until either the
    # combined transcript length reaches ``chat_batch_max_chars`` or the count
    # reaches ``chat_batch_size`` (a safety cap so many tiny conversations can't
    # explode one response). Calls are sequential per ingestion task, so these
    # are reliability knobs (smaller = less truncation/timeout risk), not
    # concurrency knobs — peak concurrency is the parallel task count.
    chat_batch_max_chars: int = 6000  # combined transcript chars per call (primary limiter)
    chat_batch_size: int = 6  # max conversations per call (safety cap)
    chat_max_tokens: int = 8192  # output budget so the combined response doesn't truncate


@dataclass
class IngestionConfig:
    """The whole ingestion config file: exclusions + LLM tuning."""

    exclusions: ExclusionFile = field(default_factory=ExclusionFile)
    llm: LlmConfig = field(default_factory=LlmConfig)


def _exclusion_file(raw: JsonValue, *, path: Path) -> ExclusionFile:
    if raw is None:
        return ExclusionFile()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = raw
    return ExclusionFile(
        phones=_str_list(payload.get("phones"), path=path),
        emails=_str_list(payload.get("emails"), path=path),
        email_domains=_str_list(payload.get("email_domains"), path=path),
        names=_str_list(payload.get("names"), path=path),
        source_ids=_str_list(payload.get("source_ids"), path=path),
        machine_unit_identifiers=_machine_unit_identifier_list(
            payload.get("machine_unit_identifiers"), path=path
        ),
    )


def _float(raw: JsonValue, default: float, *, path: Path) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return float(raw)


def _int(raw: JsonValue, default: int, *, path: Path) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return raw


def _llm_config(raw: JsonValue, *, path: Path) -> LlmConfig:
    if raw is None:
        return LlmConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = raw
    defaults = LlmConfig()
    return LlmConfig(
        timeout_seconds=_float(payload.get("timeout_seconds"), defaults.timeout_seconds, path=path),
        request_delay_seconds=_float(
            payload.get("request_delay_seconds"), defaults.request_delay_seconds, path=path
        ),
        max_retries=_int(payload.get("max_retries"), defaults.max_retries, path=path),
        retry_base_delay_seconds=_float(
            payload.get("retry_base_delay_seconds"), defaults.retry_base_delay_seconds, path=path
        ),
        retry_max_delay_seconds=_float(
            payload.get("retry_max_delay_seconds"), defaults.retry_max_delay_seconds, path=path
        ),
        chat_batch_max_chars=_int(
            payload.get("chat_batch_max_chars"), defaults.chat_batch_max_chars, path=path
        ),
        chat_batch_size=_int(payload.get("chat_batch_size"), defaults.chat_batch_size, path=path),
        chat_max_tokens=_int(payload.get("chat_max_tokens"), defaults.chat_max_tokens, path=path),
    )


def load_ingestion_config(path_value: str) -> IngestionConfig:
    """Load the consolidated ingestion config.

    Backward-compatible with the old bare-exclusions format (top-level
    exclusion keys, no ``exclusions``/``llm`` wrapper).
    """
    if not path_value.strip():
        return IngestionConfig()
    path = Path(path_value)
    if not path.exists():
        raise FileNotFoundError(f"Ingestion config file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid ingestion config JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = cast(dict[str, JsonValue], raw)
    if "exclusions" not in payload and "llm" not in payload:
        # Old format: the whole object is the exclusions block.
        return IngestionConfig(exclusions=_exclusion_file(payload, path=path), llm=LlmConfig())
    return IngestionConfig(
        exclusions=_exclusion_file(payload.get("exclusions"), path=path),
        llm=_llm_config(payload.get("llm"), path=path),
    )


def get_ingestion_config() -> IngestionConfig:
    """Load the ingestion config from the configured file path."""
    return load_ingestion_config(get_settings().ingestion_config_file)
