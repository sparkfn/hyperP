"""Contract-identifier tests for the #125 sales prediction package."""

from __future__ import annotations

import pytest
from src.sales_prediction.contracts import (
    ARTIFACT_KIND_DATASET,
    ARTIFACT_KIND_EVALUATION,
    ARTIFACT_KIND_MODEL,
    AVAILABILITY_SEMANTICS,
    DATASET_SCHEMA_VERSION,
    DEFAULT_EXPECTED_MAPPING_VERSION,
    DEFAULT_EXPECTED_POLICY_VERSION,
    ELIGIBILITY_VERSION,
    HORIZON_DAYS,
    RESTATEMENT_VERSION,
    SALES_ARTIFACT_HMAC_DOMAIN,
    SELECTOR_VERSION,
    parse_entity_keys,
)


def test_accepted_release_binding_matches_issue_149_decision() -> None:
    assert SELECTOR_VERSION == "retrospective-source-availability-v1"
    assert ELIGIBILITY_VERSION == "crm-won-retrospective-eligibility-v1"
    assert DEFAULT_EXPECTED_MAPPING_VERSION == "crm-stage-map-2026-08-18-v1"
    assert DEFAULT_EXPECTED_POLICY_VERSION == "crm-stage-lifecycle-policy-2026-08-18-v1"
    assert AVAILABILITY_SEMANTICS == "retrospective_source_native"
    assert RESTATEMENT_VERSION == "authority-head-v1"
    assert HORIZON_DAYS == 30


def test_artifact_kinds_and_schemas_are_stable() -> None:
    assert DATASET_SCHEMA_VERSION == "issue-125-crm-dataset-v1"
    assert ARTIFACT_KIND_DATASET == "sales-dataset"
    assert ARTIFACT_KIND_EVALUATION == "sales-evaluation"
    assert ARTIFACT_KIND_MODEL == "sales-model"


def test_sales_hmac_domain_is_distinct_from_bitrix_domain() -> None:
    from src.connectors.bitrix_stage_history.artifact_manifest import MANIFEST_HMAC_DOMAIN

    assert SALES_ARTIFACT_HMAC_DOMAIN.endswith(b"\x00")
    assert SALES_ARTIFACT_HMAC_DOMAIN != MANIFEST_HMAC_DOMAIN


def test_parse_entity_keys_accepts_approved_populations() -> None:
    assert parse_entity_keys("eko,fundbox,speedzone") == ("eko", "fundbox", "speedzone")
    assert parse_entity_keys(" eko , fundbox ") == ("eko", "fundbox")


def test_parse_entity_keys_rejects_invalid_input() -> None:
    with pytest.raises(ValueError, match="at least one entity key"):
        parse_entity_keys("   ")
    with pytest.raises(ValueError, match="invalid entity key"):
        parse_entity_keys("Eko")
    with pytest.raises(ValueError, match="invalid entity key"):
        parse_entity_keys("-eko")
    with pytest.raises(ValueError, match="entity keys must be unique"):
        parse_entity_keys("eko,eko")
