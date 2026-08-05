"""Tests for the issue #124 CRM-WON discovery runner boundaries."""

from __future__ import annotations

import argparse
from io import StringIO
from pathlib import Path

import pytest
from src.graph.queries.sales_prediction_discovery import DISCOVERY_DEAL_RECORDS
from src.sales_prediction_discovery import (
    DiscoveryOutput,
    DiscoverySettings,
    _main,
    _mapping_status,
    _markdown_cell,
    parse_as_of_at,
    parse_entity_keys,
    parse_non_empty,
    render_markdown,
)
from src.sales_prediction_discovery_config import EntityStagePolicy, StageMapping


def _output() -> DiscoveryOutput:
    return DiscoveryOutput(
        generated_at="2026-08-05T00:00:00Z",
        settings=DiscoverySettings(
            as_of_at="2026-08-01T00:00:00Z",
            report_cutoff_at="2026-08-05T00:00:00Z",
            entity_keys=("fundbox",),
            late_arrival_seconds=259200,
            configuration_version="issue-124-v2",
            stage_mapping=None,
        ),
        report_schema_version="issue-124-crm-won-v1",
        source_capability=[],
        source_coverage=[{"record_type": "crm_deal", "record_count": 4}],
        deal_coverage=[],
        history_capability=[],
        interaction_coverage=[],
        late_arrival=[],
        mapping_status=[{"mapping_status": "mapping_not_supplied"}],
        label_capability=[{"label_status": "label_unavailable"}],
    )


def test_parse_as_of_at_normalizes_an_explicit_offset_to_utc() -> None:
    assert parse_as_of_at("2026-08-04T08:00:00+08:00") == "2026-08-04T00:00:00Z"


@pytest.mark.parametrize("value", ["2026-08-04T00:00:00", "not-a-time"])
def test_parse_as_of_at_rejects_ambiguous_or_invalid_timestamps(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_as_of_at(value)


def test_parse_entity_keys_and_configuration_version_are_bounded() -> None:
    assert parse_entity_keys(" fundbox, speedzone, fundbox ") == ("fundbox", "speedzone")
    assert parse_non_empty(" issue-124-v2 ") == "issue-124-v2"
    with pytest.raises(argparse.ArgumentTypeError):
        parse_entity_keys(" , ")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_entity_keys("a" * 65)
    with pytest.raises(argparse.ArgumentTypeError):
        parse_non_empty("issue-124\nspoof")


def test_rendered_output_has_no_commerce_sections_and_declares_label_boundary() -> None:
    rendered = render_markdown(_output())

    assert "Report schema: issue-124-crm-won-v1" in rendered
    assert "Source coverage at report cutoff" in rendered
    assert "CRM-WON label capability" in rendered
    assert "Order coverage" not in rendered
    assert "Deal-to-order linkage" not in rendered
    assert "label, dataset, model" in rendered


def test_markdown_cell_flattens_control_lines_and_escapes_markup() -> None:
    assert _markdown_cell("<tag>\nleft|right") == "&lt;tag&gt; left\\|right"


def test_deal_query_bounds_generic_history_coverage_by_report_cutoff() -> None:
    assert "history.ingested_at <= datetime($report_cutoff_at)" in DISCOVERY_DEAL_RECORDS


def test_mapping_status_is_reported_per_requested_entity() -> None:
    mapping = StageMapping(
        policy_version="issue-124-v1",
        claimed_approval_status="draft",
        external_approval_reference=None,
        entities=(
            EntityStagePolicy(
                entity_key="fundbox",
                open_stage_ids=("OPEN",),
                won_stage_ids=("WON",),
                lost_stage_ids=(),
                excluded_stage_ids=(),
                reopen_revert_policy_status="pending",
            ),
        ),
        configuration_hash="a" * 64,
    )

    output = _mapping_status(mapping, ("fundbox", "eko"))

    assert output[0]["mapping_status"] == "mapping_supplied_unverified"
    assert output[0]["entity_mapping_present"] is True
    assert output[0]["reopen_revert_policy_status"] == "pending"
    assert output[1]["mapping_status"] == "mapping_missing_for_entity"
    assert output[1]["entity_mapping_present"] is False
    assert output[1]["reopen_revert_policy_status"] is None
    assert {tuple(row) for row in output} == {tuple(output[0])}


@pytest.mark.asyncio
async def test_main_rejects_reversed_cutoffs_before_database_access(tmp_path: Path) -> None:
    arguments = [
        "--as-of-at",
        "2026-08-04T00:00:00Z",
        "--report-cutoff-at",
        "2026-08-01T00:00:00Z",
        "--entities",
        "fundbox",
        "--configuration-version",
        "issue-124-v2",
        "--json-output",
        str(tmp_path / "output.json"),
        "--markdown-output",
        str(tmp_path / "output.md"),
    ]

    with pytest.raises(ValueError, match="must not precede"):
        await _main(arguments, StringIO())
