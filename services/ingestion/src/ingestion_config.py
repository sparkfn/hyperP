"""Consolidated JSON config for ingestion: hard exclusions + LLM call tuning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from src.config import get_settings
from src.exclusion_config import (
    ExclusionFile,
    _str_list,
    _vehicle_identifier_list,
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
    chat_extraction_retry_attempts: int = 3  # retries after the initial batch response


BitrixOpenLinesChannelType = Literal[
    "whatsapp_business_api",
    "whatsapp_device",
    "facebook_direct",
    "facebook_comments",
    "instagram",
    "telegram",
    "carousell",
    "bitrix_chat",
    "other",
]


@dataclass
class BitrixOpenLinesConfig:
    """Selection and checkpoint tuning for Bitrix Open Lines ingestion."""

    included_channel_types: list[BitrixOpenLinesChannelType] = field(
        default_factory=lambda: [
            "whatsapp_business_api",
            "facebook_direct",
            "instagram",
        ]
    )
    included_config_ids: list[str] = field(default_factory=list)
    excluded_config_ids: list[str] = field(default_factory=list)
    entity_by_config_id: dict[str, str] = field(default_factory=dict)
    entity_by_crm_category_id: dict[str, str] = field(default_factory=dict)
    incremental_overlap_seconds: int = 300
    recent_page_size: int = 50


@dataclass
class ScheduledIngestionConfig:
    """Controls publication of all scheduled API-ingestion chains."""

    enabled: bool = False


@dataclass
class IngestionConfig:
    """The whole ingestion config file: exclusions, LLM tuning, and scheduling."""

    exclusions: ExclusionFile = field(default_factory=ExclusionFile)
    llm: LlmConfig = field(default_factory=LlmConfig)
    bitrix_openlines: BitrixOpenLinesConfig = field(default_factory=BitrixOpenLinesConfig)
    scheduled_ingestion: ScheduledIngestionConfig = field(default_factory=ScheduledIngestionConfig)


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
        vehicle_identifiers=_vehicle_identifier_list(payload.get("vehicle_identifiers"), path=path),
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
        chat_extraction_retry_attempts=_int(
            payload.get("chat_extraction_retry_attempts"),
            defaults.chat_extraction_retry_attempts,
            path=path,
        ),
    )


_CHANNEL_TYPES: set[str] = {
    "whatsapp_business_api",
    "whatsapp_device",
    "facebook_direct",
    "facebook_comments",
    "instagram",
    "telegram",
    "carousell",
    "bitrix_chat",
    "other",
}


def _config_ids(raw: JsonValue, *, path: Path) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    result: list[str] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"Invalid ingestion config JSON: {path}")
        normalized = str(value).strip()
        if not normalized.isdigit():
            raise ValueError(f"Invalid ingestion config JSON: {path}")
        result.append(normalized)
    return result


def _entity_by_numeric_id(raw: JsonValue, *, path: Path) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    entity_map: dict[str, str] = {}
    for raw_id, entity_key in raw.items():
        if not isinstance(raw_id, str) or not raw_id.isdigit():
            raise ValueError(f"Invalid ingestion config JSON: {path}")
        if not isinstance(entity_key, str) or not entity_key.strip():
            raise ValueError(f"Invalid ingestion config JSON: {path}")
        entity_map[raw_id] = entity_key.strip()
    return entity_map


def _bitrix_openlines_config(raw: JsonValue, *, path: Path) -> BitrixOpenLinesConfig:
    if raw is None:
        return BitrixOpenLinesConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    defaults = BitrixOpenLinesConfig()
    raw_types = raw.get("included_channel_types")
    if raw_types is None:
        included_types = defaults.included_channel_types
    elif not isinstance(raw_types, list) or any(
        not isinstance(value, str) or value not in _CHANNEL_TYPES for value in raw_types
    ):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    else:
        included_types = cast(list[BitrixOpenLinesChannelType], list(raw_types))
    entity_map = _entity_by_numeric_id(raw.get("entity_by_config_id"), path=path)
    crm_category_entity_map = _entity_by_numeric_id(raw.get("entity_by_crm_category_id"), path=path)
    overlap = _int(
        raw.get("incremental_overlap_seconds"),
        defaults.incremental_overlap_seconds,
        path=path,
    )
    page_size = _int(raw.get("recent_page_size"), defaults.recent_page_size, path=path)
    if overlap < 0 or page_size < 1 or page_size > 50:
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return BitrixOpenLinesConfig(
        included_channel_types=included_types,
        included_config_ids=_config_ids(raw.get("included_config_ids"), path=path),
        excluded_config_ids=_config_ids(raw.get("excluded_config_ids"), path=path),
        entity_by_config_id=entity_map,
        entity_by_crm_category_id=crm_category_entity_map,
        incremental_overlap_seconds=overlap,
        recent_page_size=page_size,
    )


def _scheduled_ingestion_config(raw: JsonValue, *, path: Path) -> ScheduledIngestionConfig:
    if raw is None:
        return ScheduledIngestionConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return ScheduledIngestionConfig(enabled=enabled)


def load_ingestion_config(path_value: str) -> IngestionConfig:
    """Load the consolidated ingestion config.

    Backward-compatible with the old bare-exclusions format (top-level
    exclusion keys and no known nested configuration section).
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
    if not {"exclusions", "llm", "bitrix_openlines", "scheduled_ingestion"}.intersection(payload):
        # Old format: the whole object is the exclusions block.
        return IngestionConfig(exclusions=_exclusion_file(payload, path=path), llm=LlmConfig())
    return IngestionConfig(
        exclusions=_exclusion_file(payload.get("exclusions"), path=path),
        llm=_llm_config(payload.get("llm"), path=path),
        bitrix_openlines=_bitrix_openlines_config(payload.get("bitrix_openlines"), path=path),
        scheduled_ingestion=_scheduled_ingestion_config(
            payload.get("scheduled_ingestion"), path=path
        ),
    )


def get_ingestion_config() -> IngestionConfig:
    """Load the ingestion config from the configured file path."""
    return load_ingestion_config(get_settings().ingestion_config_file)
