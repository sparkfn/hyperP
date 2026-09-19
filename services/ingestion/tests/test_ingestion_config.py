"""Tests for the consolidated ingestion config loader."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import src.ingestion_config as ingestion_config
from src.exclusion_config import ExclusionFile
from src.ingestion_config import (
    BitrixOpenLinesConfig,
    IngestionConfig,
    LlmConfig,
    ScheduledIngestionConfig,
    StageHistoryIngestionConfig,
    bitrix_configuration_digest,
    bitrix_legacy_explicit_category_digest,
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


def test_crm_activity_ingestion_is_permanently_retired_and_not_digest_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps({"bitrix_openlines": {"crm_activity_ingestion_status": "enabled"}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))

    config = BitrixOpenLinesConfig()
    assert config.crm_activity_ingestion_status == "retired"
    captured: list[object] = []
    original_dumps = ingestion_config.json.dumps

    def capture_dumps(value: object, *args: object, **kwargs: object) -> str:
        captured.append(value)
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(ingestion_config.json, "dumps", capture_dumps)
    bitrix_configuration_digest(config, ())

    assert len(captured) == 1
    digest_input = captured[0]
    assert isinstance(digest_input, dict)
    digest_config = digest_input["config"]
    assert isinstance(digest_config, dict)
    assert "crm_activity_ingestion_status" not in digest_config


def test_legacy_explicit_category_digest_reconstructs_accepted_gate_evidence() -> None:
    categories = ("2", "7", "8")
    runtime_config = BitrixOpenLinesConfig(
        included_crm_category_ids=list(categories),
        entity_by_crm_category_id={
            "2": "eko",
            "7": "fundbox",
            "8": "speedzone",
        },
    )
    accepted_digest = bitrix_legacy_explicit_category_digest(runtime_config, categories)
    runtime_digest = bitrix_configuration_digest(runtime_config, categories)

    assert accepted_digest == (
        "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
    )
    assert accepted_digest == bitrix_configuration_digest(BitrixOpenLinesConfig(), categories)
    assert runtime_digest == (
        "sha256:a449c56111af4eff4d8d3182355d037bee51760c45fc77c1451b4cac5bb4e75a"
    )
    assert runtime_digest != accepted_digest
    assert (
        bitrix_legacy_explicit_category_digest(
            replace(runtime_config, source_instance_id="bitrix-primary"), categories
        )
        == accepted_digest
    )


def test_scheduled_ingestion_is_disabled_by_default() -> None:
    assert load_ingestion_config("").scheduled_ingestion == ScheduledIngestionConfig(enabled=False)


def test_stage_history_ingestion_is_hard_disabled_by_default() -> None:
    config = load_ingestion_config("").stage_history_ingestion

    assert config == StageHistoryIngestionConfig(enabled=False)
    with pytest.raises(PermissionError, match="disabled"):
        config.assert_dispatch_enabled(now=datetime(2026, 8, 14, tzinfo=UTC))


def test_stage_history_ingestion_parses_bounded_authorization(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "stage_history_ingestion": {
                    "enabled": True,
                    "authorization_reference": "approval-147",
                    "authorized_actor": "reviewer@example.com",
                    "authorization_expires_at": "2026-08-15T00:00:00Z",
                    "owner_artifact_id": "owner-1",
                    "owner_manifest_hmac": "a" * 64,
                    "stage_artifact_id": "stage-1",
                    "stage_manifest_hmac": "b" * 64,
                    "qualification_evidence_digest": "sha256:" + "c" * 64,
                    "accepted_configuration_digest": "sha256:" + "d" * 64,
                    "source_contract_uuid": "12345678-1234-5678-9234-567812345678",
                    "entity_type_id": 2,
                    "max_calls": 2,
                    "max_rows": 100,
                    "max_spool_bytes": 1000000,
                    "max_runtime_seconds": 120.0,
                    "retention_days": 7,
                    "retry_max_attempts": 4,
                    "retry_backoff_seconds": 180,
                    "review_lease_seconds": 600,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_ingestion_config(str(path)).stage_history_ingestion

    assert config.enabled is True
    assert config.entity_type_id == 2
    assert config.max_rows == 100
    assert config.retry_backoff_seconds == 180
    config.assert_dispatch_enabled(now=datetime(2026, 8, 14, tzinfo=UTC))


@pytest.mark.parametrize(
    "override",
    [
        {"authorization_reference": ""},
        {"authorization_expires_at": "2026-08-15"},
        {"entity_type_id": 0},
        {"max_calls": 0},
        {"max_rows": 49},
        {"max_runtime_seconds": float("inf")},
        {"retry_backoff_seconds": 0},
    ],
)
def test_enabled_stage_history_requires_complete_finite_bounds(
    tmp_path: Path,
    override: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "enabled": True,
        "authorization_reference": "approval-147",
        "authorized_actor": "reviewer@example.com",
        "authorization_expires_at": "2026-08-15T00:00:00Z",
        "owner_artifact_id": "owner-1",
        "owner_manifest_hmac": "a" * 64,
        "stage_artifact_id": "stage-1",
        "stage_manifest_hmac": "b" * 64,
        "qualification_evidence_digest": "sha256:" + "c" * 64,
        "accepted_configuration_digest": "sha256:" + "d" * 64,
        "source_contract_uuid": "12345678-1234-5678-9234-567812345678",
        "entity_type_id": 2,
        "max_calls": 1,
        "max_rows": 50,
        "max_spool_bytes": 1000000,
        "max_runtime_seconds": 120.0,
        "retention_days": 7,
        "retry_max_attempts": 4,
        "retry_backoff_seconds": 180,
        "review_lease_seconds": 600,
    }
    payload.update(override)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"stage_history_ingestion": payload}), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


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
                    "source_instance_id": "bitrix-primary",
                    "standalone_crm_identity_enabled": True,
                    "included_channel_types": ["facebook_direct"],
                    "included_config_ids": [46],
                    "excluded_config_ids": [54],
                    "entity_by_config_id": {"46": "speedzone"},
                    "included_crm_category_ids": [0, 2],
                    "entity_by_crm_category_id": {"0": "eko", "2": "speedzone"},
                    "incremental_overlap_seconds": 120,
                    "recent_page_size": 25,
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_ingestion_config(str(path)).bitrix_openlines == BitrixOpenLinesConfig(
        source_instance_id="bitrix-primary",
        standalone_crm_identity_enabled=True,
        included_channel_types=["facebook_direct"],
        included_config_ids=["46"],
        excluded_config_ids=["54"],
        entity_by_config_id={"46": "speedzone"},
        included_crm_category_ids=["0", "2"],
        entity_by_crm_category_id={"0": "eko", "2": "speedzone"},
        incremental_overlap_seconds=120,
        recent_page_size=25,
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"standalone_crm_identity_enabled": True},
        {
            "source_instance_id": "bitrix-primary",
            "standalone_crm_identity_enabled": "true",
        },
    ],
)
def test_enabled_standalone_crm_identity_requires_registered_boolean_config(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"bitrix_openlines": payload}), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


@pytest.mark.parametrize(
    "source_instance_id",
    ["", " bitrix-primary ", "https://portal.test", 123],
)
def test_bitrix_openlines_config_rejects_invalid_source_instance_ids(
    tmp_path: Path,
    source_instance_id: object,
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"bitrix_openlines": {"source_instance_id": source_instance_id}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


def test_bitrix_configuration_digest_preserves_legacy_evidence_without_registration() -> None:
    categories = ("2", "7", "8")
    legacy_digest = bitrix_configuration_digest(BitrixOpenLinesConfig(), categories)

    assert legacy_digest == (
        "sha256:24ad8341df1613f75207dd5b9fab8c739e6ac162e12f64e1713c8114a565fd04"
    )
    assert (
        bitrix_configuration_digest(
            BitrixOpenLinesConfig(source_instance_id="bitrix-primary"), categories
        )
        != legacy_digest
    )


def test_standalone_identity_enablement_does_not_change_existing_stream_digest() -> None:
    categories = ("2", "7", "8")
    base = BitrixOpenLinesConfig(source_instance_id="bitrix-primary")
    enabled = replace(base, standalone_crm_identity_enabled=True)

    assert bitrix_configuration_digest(enabled, categories) == bitrix_configuration_digest(
        base, categories
    )


def test_bitrix_openlines_config_deduplicates_crm_category_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "bitrix_openlines": {
                    "included_crm_category_ids": [2, "7", 2, "7"],
                    "entity_by_crm_category_id": {"2": "eko", "7": "fundbox"},
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_ingestion_config(str(path)).bitrix_openlines

    assert config.included_crm_category_ids == ["2", "7"]


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


def test_bitrix_openlines_config_requires_mappings_for_included_crm_categories(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ingestion-config.json"
    path.write_text(
        json.dumps(
            {
                "bitrix_openlines": {
                    "included_crm_category_ids": ["2"],
                    "entity_by_crm_category_id": {"7": "fundbox"},
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="included CRM categories require entity mappings: 2"):
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


def test_standalone_identity_schedule_and_budgets_are_validated_and_excluded_from_deal_digest(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ingestion.json"
    path.write_text(
        json.dumps(
            {
                "bitrix_openlines": {
                    "source_instance_id": "bitrix-primary",
                    "standalone_crm_identity_enabled": True,
                    "standalone_crm_identity_schedule_enabled": True,
                    "standalone_crm_identity_kinds": ["lead", "contact"],
                    "standalone_crm_identity_max_calls_per_attempt": 23,
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_ingestion_config(str(path)).bitrix_openlines
    assert config.standalone_crm_identity_schedule_enabled is True
    assert config.standalone_crm_identity_kinds == ["lead", "contact"]
    assert config.standalone_crm_identity_max_calls_per_attempt == 23

    changed = replace(config, standalone_crm_identity_max_calls_per_attempt=24)
    assert bitrix_configuration_digest(config, ()) == bitrix_configuration_digest(changed, ())


@pytest.mark.parametrize(
    "contract_version",
    ["", "crm-company-membership-snapshot-v2", 1, True],
)
def test_standalone_identity_rejects_unsupported_association_contract_version(
    tmp_path: Path,
    contract_version: object,
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {"bitrix_openlines": {"crm_identity_association_contract_version": contract_version}}
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


@pytest.mark.parametrize(
    "limits",
    [
        {
            "standalone_crm_identity_max_rows_per_attempt": 11,
            "standalone_crm_identity_max_rows_per_occurrence": 10,
        },
        {
            "standalone_crm_identity_max_calls_per_attempt": 11,
            "standalone_crm_identity_max_calls_per_occurrence": 10,
        },
        {
            "standalone_crm_identity_max_runtime_seconds_per_attempt": 11.0,
            "standalone_crm_identity_max_wall_clock_seconds_per_occurrence": 10.0,
        },
    ],
)
def test_standalone_identity_attempt_limits_cannot_exceed_occurrence_limits(
    tmp_path: Path,
    limits: dict[str, object],
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"bitrix_openlines": limits}), encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


def test_standalone_identity_schedule_requires_global_identity_enablement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "bitrix_openlines": {
                    "source_instance_id": "bitrix-primary",
                    "standalone_crm_identity_schedule_enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


@pytest.mark.parametrize("kind_value", [1, True, {"kind": "contact"}, ["contact"]])
def test_standalone_identity_kinds_reject_non_string_elements(
    tmp_path: Path,
    kind_value: object,
) -> None:
    path = tmp_path / "bad-kinds.json"
    path.write_text(
        json.dumps({"bitrix_openlines": {"standalone_crm_identity_kinds": [kind_value]}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))


def test_mapping_authorization_grants_are_default_off_and_parse_only_complete_exact_entries(
    tmp_path: Path,
) -> None:
    assert load_ingestion_config("").crm_tenant_mapping_authorization.grants == ()
    path = tmp_path / "mapping-grant.json"
    digest = "sha256:" + "a" * 64
    path.write_text(
        json.dumps(
            {
                "crm_tenant_mapping_authorization": {
                    "grants": [
                        {
                            "action": "prepare",
                            "source_key": "bitrix_chat",
                            "source_instance_id": "portal-a",
                            "control_instance_id": "control-a",
                            "preparation_request_id": "prepare-a",
                            "manifest_digest": digest,
                            "target_entity_keys": ["entity-a"],
                            "expected_head": None,
                            "actor": "reviewer",
                            "authorization_reference": "case-a",
                            "authorization_digest": digest,
                            "expires_at": "2099-01-01T00:00:00Z",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    grant = load_ingestion_config(str(path)).crm_tenant_mapping_authorization.grants[0]

    assert grant.action == "prepare"
    assert grant.preparation_request_id == "prepare-a"


def test_mapping_authorization_rejects_partial_or_pattern_like_grants(tmp_path: Path) -> None:
    path = tmp_path / "bad-mapping-grant.json"
    path.write_text(
        json.dumps(
            {
                "crm_tenant_mapping_authorization": {
                    "grants": [{"action": "prepare", "source_key": "bitrix_chat"}]
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid ingestion config JSON"):
        load_ingestion_config(str(path))
