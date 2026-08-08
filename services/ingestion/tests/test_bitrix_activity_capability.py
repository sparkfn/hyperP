"""Strict Bitrix activity keyset capability tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_crm.activity_probe import (
    freeze_activity_upper_id,
    verify_activity_keyset,
)
from src.connectors.bitrix_openlines.models import CrmActivity, CrmActivityCapabilityPage
from src.connectors.bitrix_stage_history.models import ProbeLimits


class _Client:
    def __init__(self, pages: list[CrmActivityCapabilityPage]) -> None:
        self.pages = pages
        self.calls = 0

    def list_crm_activity_capability_page(
        self,
        *,
        greater_than_id: int | None,
        less_than_or_equal_to_id: int,
        order_direction: str = "ASC",
    ) -> CrmActivityCapabilityPage:
        del greater_than_id, less_than_or_equal_to_id, order_direction
        page = self.pages[self.calls]
        self.calls += 1
        return page


def _activity(value: int) -> CrmActivity:
    return CrmActivity(
        id=str(value),
        owner_type="2",
        owner_id="7",
        history_kind="call",
        subject=None,
        observed_at=datetime(2026, 8, 8, tzinfo=UTC),
        start_at=None,
        end_at=None,
        duration_seconds=None,
        direction=None,
        outcome=None,
        is_call=True,
        raw_payload={"ID": str(value), "OWNER_ID": "7"},
    )


def _limits() -> ProbeLimits:
    return ProbeLimits(5, 200, 1_000_000, 5.0, 2, 2)


def test_activity_probe_freezes_and_verifies_strict_keyset() -> None:
    boundary = _Client([CrmActivityCapabilityPage((_activity(9), _activity(8)), 2, None, None)])
    traversal = _Client([CrmActivityCapabilityPage((_activity(8), _activity(9)), 2, None, None)])

    assert freeze_activity_upper_id(boundary) == 9
    report = verify_activity_keyset(traversal, upper_activity_id=9, limits=_limits())

    assert report.traversal_outcome == "verified_activity_keyset"
    assert report.rows == 2


def test_activity_probe_fails_closed_on_mutable_or_unordered_pages() -> None:
    with pytest.raises(RuntimeError, match="strictly increasing"):
        verify_activity_keyset(
            _Client([CrmActivityCapabilityPage((_activity(2), _activity(1)), 2, None, None)]),
            upper_activity_id=2,
            limits=_limits(),
        )
