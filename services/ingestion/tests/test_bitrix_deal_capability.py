from __future__ import annotations

from pathlib import Path

import pytest
from src.connectors.bitrix_openlines.models import CrmDealCapabilityItem, CrmDealCapabilityPage
from src.connectors.bitrix_stage_history.deal_probe import (
    collect_deal_owner_pass,
    deal_manifests_are_identical,
    freeze_deal_upper_id,
)
from src.connectors.bitrix_stage_history.models import ProbeLimits


class _Client:
    def __init__(self, pages: list[CrmDealCapabilityPage]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def list_crm_deal_capability_page(
        self,
        *,
        category_ids: object,
        greater_than_id: int | None = None,
        less_than_or_equal_to_id: int | None = None,
        order_direction: str = "ASC",
    ) -> CrmDealCapabilityPage:
        self.calls.append(
            {
                "category_ids": category_ids,
                "greater_than_id": greater_than_id,
                "less_than_or_equal_to_id": less_than_or_equal_to_id,
                "order_direction": order_direction,
            }
        )
        return self.pages[len(self.calls) - 1]


def _item(value: int, category: str = "2") -> CrmDealCapabilityItem:
    return CrmDealCapabilityItem(str(value), category, "C2:NEW")


def _limits() -> ProbeLimits:
    return ProbeLimits(3, 100, 1_000_000, 5.0, 2, 2)


def test_freeze_deal_upper_id_uses_descending_capability_boundary() -> None:
    client = _Client([CrmDealCapabilityPage((_item(99), _item(98)), None, 99, None, None)])

    assert freeze_deal_upper_id(client, ["2"]) == 99
    assert client.calls == [
        {
            "category_ids": ["2"],
            "greater_than_id": None,
            "less_than_or_equal_to_id": None,
            "order_direction": "DESC",
        }
    ]


def test_freeze_deal_upper_id_rejects_unordered_or_duplicate_boundary_rows() -> None:
    with pytest.raises(RuntimeError, match="not descending"):
        freeze_deal_upper_id(
            _Client(
                [CrmDealCapabilityPage((_item(99), _item(1), _item(98)), None, 99, None, None)]
            ),
            ["2"],
        )
    with pytest.raises(RuntimeError, match="not descending"):
        freeze_deal_upper_id(
            _Client([CrmDealCapabilityPage((_item(99), _item(99)), None, 99, None, None)]),
            ["2"],
        )


def test_collects_strict_bounded_keyset_owner_manifest(tmp_path: Path) -> None:
    client = _Client(
        [
            CrmDealCapabilityPage(
                tuple(_item(index) for index in range(1, 51)), None, None, None, None
            ),
            CrmDealCapabilityPage((_item(51),), None, None, None, None),
        ]
    )
    manifest, spool = collect_deal_owner_pass(
        client,
        category_ids=["2"],
        upper_deal_id=99,
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert client.calls == [
        {
            "category_ids": ["2"],
            "greater_than_id": None,
            "less_than_or_equal_to_id": 99,
            "order_direction": "ASC",
        },
        {
            "category_ids": ["2"],
            "greater_than_id": 50,
            "less_than_or_equal_to_id": 99,
            "order_direction": "ASC",
        },
    ]
    assert manifest.unique_owner_rows == 51
    assert manifest.raw_rows == 51
    assert manifest.upper_deal_id_digest.startswith("sha256:")
    assert spool.path.stat().st_mode & 0o077 == 0
    spool.delete()


def test_rejects_rows_above_frozen_boundary(tmp_path: Path) -> None:
    client = _Client([CrmDealCapabilityPage((_item(100),), None, None, None, None)])

    with pytest.raises(RuntimeError, match="frozen upper boundary"):
        collect_deal_owner_pass(
            client,
            category_ids=["2"],
            upper_deal_id=99,
            limits=_limits(),
            spool_directory=tmp_path / "restricted",
            pass_number=1,
        )


def test_manifest_comparison_requires_owner_set_and_frozen_boundary(tmp_path: Path) -> None:
    first, first_spool = collect_deal_owner_pass(
        _Client([CrmDealCapabilityPage((_item(1),), None, 1, None, None)]),
        category_ids=["2"],
        upper_deal_id=1,
        limits=_limits(),
        spool_directory=tmp_path / "one",
        pass_number=1,
    )
    second, second_spool = collect_deal_owner_pass(
        _Client([CrmDealCapabilityPage((_item(1),), None, 1, None, None)]),
        category_ids=["2"],
        upper_deal_id=1,
        limits=_limits(),
        spool_directory=tmp_path / "two",
        pass_number=1,
    )

    assert deal_manifests_are_identical(first, second)
    first_spool.delete()
    second_spool.delete()


def test_total_metadata_change_from_none_is_inconsistent(tmp_path: Path) -> None:
    client = _Client(
        [
            CrmDealCapabilityPage(
                tuple(_item(index) for index in range(1, 51)), None, None, None, None
            ),
            CrmDealCapabilityPage((_item(51),), None, 51, None, None),
        ]
    )

    manifest, spool = collect_deal_owner_pass(
        client,
        category_ids=["2"],
        upper_deal_id=99,
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert manifest.source_total_consistent is False
    assert manifest.source_total_matches_rows is None
    spool.delete()


def test_owner_census_redacts_boundaries_and_manifests_with_one_run_key(tmp_path: Path) -> None:
    run_key = b"a" * 32
    different_run_key = b"b" * 32
    first, first_spool = collect_deal_owner_pass(
        _Client([CrmDealCapabilityPage((_item(1),), None, 1, None, None)]),
        category_ids=["2"],
        upper_deal_id=1,
        limits=_limits(),
        spool_directory=tmp_path / "first",
        pass_number=1,
        redaction_key=run_key,
    )
    same_key, same_key_spool = collect_deal_owner_pass(
        _Client([CrmDealCapabilityPage((_item(1),), None, 1, None, None)]),
        category_ids=["2"],
        upper_deal_id=1,
        limits=_limits(),
        spool_directory=tmp_path / "same-key",
        pass_number=1,
        redaction_key=run_key,
    )
    different_key, different_key_spool = collect_deal_owner_pass(
        _Client([CrmDealCapabilityPage((_item(1),), None, 1, None, None)]),
        category_ids=["2"],
        upper_deal_id=1,
        limits=_limits(),
        spool_directory=tmp_path / "different-key",
        pass_number=1,
        redaction_key=different_run_key,
    )

    assert first.upper_deal_id_digest.startswith("hmac-sha256:")
    assert first.owner_manifest_digest.startswith("hmac-sha256:")
    assert first.category_inventory_digest.startswith("hmac-sha256:")
    assert deal_manifests_are_identical(first, same_key)
    assert first.upper_deal_id_digest != different_key.upper_deal_id_digest
    assert first.owner_manifest_digest != different_key.owner_manifest_digest

    first_spool.delete()
    same_key_spool.delete()
    different_key_spool.delete()
