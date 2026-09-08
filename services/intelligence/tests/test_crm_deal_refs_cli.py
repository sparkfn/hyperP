"""Exact nested CLI shape and unsafe argument rejection."""

from __future__ import annotations

import pytest
from intelligence.cli import build_parser
from intelligence.crm_deal_refs.models import parse_cutoff


def test_parser_accepts_only_fixed_nested_deal_reference_actions() -> None:
    parser = build_parser()
    arguments = parser.parse_args(
        (
            "crm",
            "deal-refs",
            "extract",
            "--source-instance-id",
            "one",
            "--as-of",
            "2026-01-01T00:00:00Z",
            "--max-records",
            "1",
        )
    )
    assert arguments.command == "crm"
    assert arguments.deal_refs_command == "extract"
    with pytest.raises(SystemExit):
        parser.parse_args(("crm", "deal-refs", "extract", "--shell", "x"))


@pytest.mark.parametrize("value", ("2026-01-01T00:00:00", "2999-01-01T00:00:00Z"))
def test_cutoff_requires_non_future_timezone_aware_value(value: str) -> None:
    with pytest.raises(ValueError):
        parse_cutoff(value)
