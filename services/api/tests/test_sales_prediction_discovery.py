"""Tests for the issue #124 discovery runner's pure boundaries."""

from __future__ import annotations

import argparse
from io import StringIO
from pathlib import Path

import pytest
from src.sales_prediction_discovery import (
    DiscoveryOutput,
    DiscoverySettings,
    _main,
    _markdown_cell,
    parse_as_of_at,
    parse_entity_keys,
    parse_non_empty,
    render_markdown,
)


def test_parse_as_of_at_normalizes_an_explicit_offset_to_utc() -> None:
    assert parse_as_of_at("2026-08-04T08:00:00+08:00") == "2026-08-04T00:00:00Z"


@pytest.mark.parametrize("value", ["2026-08-04T00:00:00", "not-a-time"])
def test_parse_as_of_at_rejects_ambiguous_or_invalid_timestamps(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        parse_as_of_at(value)


def test_parse_entity_keys_trims_and_rejects_empty_values() -> None:
    assert parse_entity_keys(" fundbox, speedzone, fundbox ") == ("fundbox", "speedzone")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_entity_keys(" , ")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_entity_keys("fundbox\nspoof")


def test_parse_non_empty_normalizes_and_rejects_blank_configuration_versions() -> None:
    assert parse_non_empty(" issue-124-v1 ") == "issue-124-v1"
    with pytest.raises(argparse.ArgumentTypeError):
        parse_non_empty("   ")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_non_empty("issue-124\nspoof")


def test_render_markdown_contains_only_aggregate_rows_and_boundary() -> None:
    output = DiscoveryOutput(
        generated_at="2026-08-04T00:00:00Z",
        settings=DiscoverySettings(
            as_of_at="2026-08-01T00:00:00Z",
            entity_keys=("fundbox",),
            late_arrival_seconds=259200,
            configuration_version="issue-124-v1",
        ),
        source_coverage=[{"record_type": "crm_deal", "record_count": 4}],
        deal_coverage=[],
        interaction_coverage=[],
        order_coverage=[],
        deal_order_linkage=[],
        late_arrival=[],
    )

    rendered = render_markdown(output)

    assert "| record_type | record_count |" in rendered
    assert "crm_deal" in rendered
    assert "Interpretation boundary" in rendered
    assert "go`, `collect_more_data`, `rules_only`, or `stop`" in rendered


def test_markdown_cell_flattens_control_lines_and_escapes_markup() -> None:
    assert _markdown_cell("<tag>\nleft|right") == "&lt;tag&gt; left\\|right"


@pytest.mark.asyncio
async def test_main_rejects_future_cutoff_before_database_access(tmp_path: Path) -> None:
    arguments = [
        "--as-of-at",
        "2999-01-01T00:00:00Z",
        "--entities",
        "fundbox",
        "--configuration-version",
        "issue-124-v1",
        "--json-output",
        str(tmp_path / "output.json"),
        "--markdown-output",
        str(tmp_path / "output.md"),
    ]

    with pytest.raises(ValueError, match="must not be in the future"):
        await _main(arguments, StringIO())


@pytest.mark.asyncio
async def test_main_rejects_same_output_path_before_database_access(tmp_path: Path) -> None:
    output = tmp_path / "output"
    arguments = [
        "--as-of-at",
        "2026-08-01T00:00:00Z",
        "--entities",
        "fundbox",
        "--configuration-version",
        "issue-124-v1",
        "--json-output",
        str(output),
        "--markdown-output",
        str(output),
    ]

    with pytest.raises(ValueError, match="output paths must be different"):
        await _main(arguments, StringIO())
