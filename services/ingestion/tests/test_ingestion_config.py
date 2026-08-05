"""Tests for the consolidated ingestion config loader."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.exclusion_config import ExclusionFile
from src.ingestion_config import (
    BitrixOpenLinesConfig,
    IngestionConfig,
    LlmConfig,
    ScheduledIngestionConfig,
    load_ingestion_config,
)


def test_bitrix_openlines_defaults_select_safe_channel_types() -> None:
    config = load_ingestion_config("")

    assert config.bitrix_openlines == BitrixOpenLinesConfig(
        included_channel_types=[
            "whatsapp_business_api",
            "facebook_direct",
            "instagram",
        ]
    )


def test_scheduled_ingestion_is_disabled_by_default() -> None:
    assert load_ingestion_config("").scheduled_ingestion == ScheduledIngestionConfig(enabled=False)


def test_scheduled_ingestion_config_parses_explicit_enablement(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps({"scheduled_ingestion": {"enabled": True}}),
        encoding="utf-8",
    )
    assert load_ingestion_config(str(path)).scheduled_ingestion == ScheduledIngestionConfig(
        enabled=True
    )


def test_empty_scheduled_ingestion_section_defaults_to_disabled(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(json.dumps({"scheduled_ingestion": {}}), encoding="utf-8")

    assert load_ingestion_config(str(path)).scheduled_ingestion == ScheduledIngestionConfig(
        enabled=False
    )


@pytest.mark.parametrize("enabled", ["true", 1, None])
def test_scheduled_ingestion_requires_a_boolean_enabled_value(
    tmp_path: Path,
    enabled: object,
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"scheduled_ingestion": {"enabled": enabled}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


def test_bitrix_openlines_config_parses_channel_and_entity_overrides(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "bitrix_openlines": {
                    "included_channel_types": ["facebook_direct"],
                    "included_config_ids": [46],
                    "excluded_config_ids": [54],
                    "entity_by_config_id": {"46": "speedzone"},
                    "entity_by_crm_category_id": {"0": "eko", "2": "speedzone"},
                    "incremental_overlap_seconds": 120,
                    "recent_page_size": 25,
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_ingestion_config(str(path)).bitrix_openlines == BitrixOpenLinesConfig(
        included_channel_types=["facebook_direct"],
        included_config_ids=["46"],
        excluded_config_ids=["54"],
        entity_by_config_id={"46": "speedzone"},
        entity_by_crm_category_id={"0": "eko", "2": "speedzone"},
        incremental_overlap_seconds=120,
        recent_page_size=25,
    )


@pytest.mark.parametrize(
    "crm_category_map",
    [
        {"category": "eko"},
        {"2": "   "},
    ],
)
def test_bitrix_openlines_config_rejects_invalid_crm_category_entity_mappings(
    tmp_path: Path,
    crm_category_map: dict[str, str],
) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "bitrix_openlines": {
                    "entity_by_crm_category_id": crm_category_map,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


def test_nested_format_parses_exclusions_and_llm(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "exclusions": {
                    "phones": ["+6511111111"],
                    "emails": ["a@b.com"],
                    "email_domains": ["b.com"],
                    "names": ["Acme"],
                    "source_ids": ["s1"],
                    "vehicle_identifiers": [{"vehicle_product": "Forklift X"}],
                },
                "llm": {
                    "timeout_seconds": 12.0,
                    "request_delay_seconds": 0.25,
                    "max_retries": 3,
                    "retry_base_delay_seconds": 0.5,
                    "retry_max_delay_seconds": 10.0,
                    "chat_batch_max_chars": 1234,
                    "chat_batch_size": 3,
                    "chat_extraction_retry_attempts": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_ingestion_config(str(path))
    assert config.exclusions.phones == ["+6511111111"]
    assert config.exclusions.vehicle_identifiers == [{"vehicle_product": "Forklift X"}]
    assert config.llm == LlmConfig(
        timeout_seconds=12.0,
        request_delay_seconds=0.25,
        max_retries=3,
        retry_base_delay_seconds=0.5,
        retry_max_delay_seconds=10.0,
        chat_batch_max_chars=1234,
        chat_batch_size=3,
        chat_extraction_retry_attempts=2,
    )


def test_bare_exclusions_format_is_backward_compatible(tmp_path: Path) -> None:
    # Old format: top-level exclusion keys, no "exclusions"/"llm" wrapper.
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"phones": ["+6522222222"], "emails": []}), encoding="utf-8")
    config = load_ingestion_config(str(path))
    assert config.exclusions.phones == ["+6522222222"]
    assert config.llm == LlmConfig()  # defaults


def test_blank_path_returns_defaults() -> None:
    config = load_ingestion_config("")
    assert config == IngestionConfig(exclusions=ExclusionFile(), llm=LlmConfig())


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_ingestion_config(str(tmp_path / "nope.json"))


def test_invalid_llm_block_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"exclusions": {}, "llm": {"max_retries": "lots"}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))
