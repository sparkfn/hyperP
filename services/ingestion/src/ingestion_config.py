"""Consolidated JSON config for ingestion: hard exclusions + LLM call tuning."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
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
    included_crm_category_ids: list[str] = field(default_factory=list)
    entity_by_crm_category_id: dict[str, str] = field(default_factory=dict)
    incremental_overlap_seconds: int = 300
    recent_page_size: int = 50


@dataclass
class ScheduledIngestionConfig:
    """Controls publication of all scheduled API-ingestion chains."""

    enabled: bool = False


@dataclass(frozen=True)
class StageHistoryIngestionConfig:
    """Default-off authorization and finite limits for the #147 smoke path."""

    enabled: bool = False
    authorization_reference: str = ""
    authorized_actor: str = ""
    authorization_expires_at: datetime | None = None
    owner_artifact_id: str = ""
    owner_manifest_hmac: str = ""
    stage_artifact_id: str = ""
    stage_manifest_hmac: str = ""
    qualification_evidence_digest: str = ""
    accepted_configuration_digest: str = ""
    source_contract_uuid: str = ""
    entity_type_id: int = 0
    max_calls: int = 1
    max_rows: int = 50
    max_spool_bytes: int = 10_000_000
    max_runtime_seconds: float = 300.0
    retention_days: int = 7
    retry_max_attempts: int = 5
    retry_backoff_seconds: int = 300
    review_lease_seconds: int = 900

    def assert_dispatch_enabled(self, *, now: datetime) -> None:
        """Fail closed before any source call, task publication, or graph write."""
        if not self.enabled:
            raise PermissionError("stage-history ingestion is disabled")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("stage-history authorization check time must be timezone-aware")
        if self.authorization_expires_at is None or now >= self.authorization_expires_at:
            raise PermissionError("stage-history ingestion authorization has expired")


@dataclass
class IngestionConfig:
    """The whole ingestion config file: exclusions, LLM tuning, and scheduling."""

    exclusions: ExclusionFile = field(default_factory=ExclusionFile)
    llm: LlmConfig = field(default_factory=LlmConfig)
    bitrix_openlines: BitrixOpenLinesConfig = field(default_factory=BitrixOpenLinesConfig)
    scheduled_ingestion: ScheduledIngestionConfig = field(default_factory=ScheduledIngestionConfig)
    stage_history_ingestion: StageHistoryIngestionConfig = field(
        default_factory=StageHistoryIngestionConfig
    )


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
    seen: set[str] = set()
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise ValueError(f"Invalid ingestion config JSON: {path}")
        normalized = str(value).strip()
        if not normalized.isdigit():
            raise ValueError(f"Invalid ingestion config JSON: {path}")
        if normalized not in seen:
            seen.add(normalized)
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
    included_crm_category_ids = _config_ids(raw.get("included_crm_category_ids"), path=path)
    crm_category_entity_map = _entity_by_numeric_id(raw.get("entity_by_crm_category_id"), path=path)
    missing_category_mappings = [
        category_id
        for category_id in included_crm_category_ids
        if category_id not in crm_category_entity_map
    ]
    if missing_category_mappings:
        formatted_ids = ", ".join(sorted(set(missing_category_mappings)))
        raise ValueError(
            "Invalid ingestion config JSON: "
            f"{path}: included CRM categories require entity mappings: {formatted_ids}"
        )
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
        included_crm_category_ids=included_crm_category_ids,
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


def _optional_datetime(raw: JsonValue, *, path: Path) -> datetime | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid ingestion config JSON: {path}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return parsed


def _required_text(payload: dict[str, JsonValue], key: str, *, path: Path) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    return value.strip()


def _stage_history_ingestion_config(raw: JsonValue, *, path: Path) -> StageHistoryIngestionConfig:
    if raw is None:
        return StageHistoryIngestionConfig()
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    payload = raw
    enabled = payload.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    defaults = StageHistoryIngestionConfig()
    config = StageHistoryIngestionConfig(
        enabled=enabled,
        authorization_reference=str(payload.get("authorization_reference") or "").strip(),
        authorized_actor=str(payload.get("authorized_actor") or "").strip(),
        authorization_expires_at=_optional_datetime(
            payload.get("authorization_expires_at"), path=path
        ),
        owner_artifact_id=str(payload.get("owner_artifact_id") or "").strip(),
        owner_manifest_hmac=str(payload.get("owner_manifest_hmac") or "").strip(),
        stage_artifact_id=str(payload.get("stage_artifact_id") or "").strip(),
        stage_manifest_hmac=str(payload.get("stage_manifest_hmac") or "").strip(),
        qualification_evidence_digest=str(
            payload.get("qualification_evidence_digest") or ""
        ).strip(),
        accepted_configuration_digest=str(
            payload.get("accepted_configuration_digest") or ""
        ).strip(),
        source_contract_uuid=str(payload.get("source_contract_uuid") or "").strip(),
        entity_type_id=_int(payload.get("entity_type_id"), defaults.entity_type_id, path=path),
        max_calls=_int(payload.get("max_calls"), defaults.max_calls, path=path),
        max_rows=_int(payload.get("max_rows"), defaults.max_rows, path=path),
        max_spool_bytes=_int(payload.get("max_spool_bytes"), defaults.max_spool_bytes, path=path),
        max_runtime_seconds=_float(
            payload.get("max_runtime_seconds"), defaults.max_runtime_seconds, path=path
        ),
        retention_days=_int(payload.get("retention_days"), defaults.retention_days, path=path),
        retry_max_attempts=_int(
            payload.get("retry_max_attempts"), defaults.retry_max_attempts, path=path
        ),
        retry_backoff_seconds=_int(
            payload.get("retry_backoff_seconds"), defaults.retry_backoff_seconds, path=path
        ),
        review_lease_seconds=_int(
            payload.get("review_lease_seconds"), defaults.review_lease_seconds, path=path
        ),
    )
    positive_values = (
        config.max_calls,
        config.max_rows,
        config.max_spool_bytes,
        config.max_runtime_seconds,
        config.retention_days,
        config.retry_max_attempts,
        config.retry_backoff_seconds,
        config.review_lease_seconds,
    )
    if (
        any(value <= 0 for value in positive_values)
        or not math.isfinite(config.max_runtime_seconds)
        or config.max_rows < 50
    ):
        raise ValueError(f"Invalid ingestion config JSON: {path}")
    if enabled:
        for key in (
            "authorization_reference",
            "authorized_actor",
            "owner_artifact_id",
            "owner_manifest_hmac",
            "stage_artifact_id",
            "stage_manifest_hmac",
            "qualification_evidence_digest",
            "accepted_configuration_digest",
            "source_contract_uuid",
        ):
            _required_text(payload, key, path=path)
        if config.authorization_expires_at is None or config.entity_type_id < 1:
            raise ValueError(f"Invalid ingestion config JSON: {path}")
    return config


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
    if not {
        "exclusions",
        "llm",
        "bitrix_openlines",
        "scheduled_ingestion",
        "stage_history_ingestion",
    }.intersection(payload):
        # Old format: the whole object is the exclusions block.
        return IngestionConfig(exclusions=_exclusion_file(payload, path=path), llm=LlmConfig())
    return IngestionConfig(
        exclusions=_exclusion_file(payload.get("exclusions"), path=path),
        llm=_llm_config(payload.get("llm"), path=path),
        bitrix_openlines=_bitrix_openlines_config(payload.get("bitrix_openlines"), path=path),
        scheduled_ingestion=_scheduled_ingestion_config(
            payload.get("scheduled_ingestion"), path=path
        ),
        stage_history_ingestion=_stage_history_ingestion_config(
            payload.get("stage_history_ingestion"), path=path
        ),
    )


def bitrix_configuration_digest(
    config: BitrixOpenLinesConfig,
    included_category_ids: tuple[str, ...],
) -> str:
    """Hash the effective non-secret Bitrix runtime selection configuration."""
    encoded = json.dumps(
        {
            "categories": included_category_ids,
            "config": asdict(config),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def bitrix_legacy_explicit_category_digest(
    config: BitrixOpenLinesConfig,
    included_category_ids: tuple[str, ...],
) -> str:
    """Reconstruct v1 evidence where CRM scope came only from CLI categories."""
    legacy_config = replace(
        config,
        included_crm_category_ids=[],
        entity_by_crm_category_id={},
    )
    return bitrix_configuration_digest(legacy_config, included_category_ids)


def get_ingestion_config() -> IngestionConfig:
    """Load the ingestion config from the configured file path."""
    return load_ingestion_config(get_settings().ingestion_config_file)
