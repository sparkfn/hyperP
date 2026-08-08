from __future__ import annotations

from src.connectors.bitrix_openlines.models import CrmDealStageCatalogItem, CrmDealStageCatalogPage
from src.connectors.bitrix_stage_history.catalog_probe import collect_current_stage_catalog
from src.connectors.bitrix_stage_history.models import ProbeLimits


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def list_crm_deal_stage_catalog_page(
        self, *, category_id: int, start: int = 0
    ) -> CrmDealStageCatalogPage:
        self.calls.append((category_id, start))
        if category_id == 2:
            return CrmDealStageCatalogPage(
                (CrmDealStageCatalogItem("2", "C2:NEW", "process"),), None, 1, 0.1, 10.0
            )
        return CrmDealStageCatalogPage(
            (CrmDealStageCatalogItem("3", "C3:NEW", "process"),), None, 1, 0.1, 10.0
        )


def test_collects_bounded_current_catalog_with_keyed_digest() -> None:
    client = _Client()
    manifest, keys = collect_current_stage_catalog(
        client,
        category_ids=("3", "2"),
        limits=ProbeLimits(3, 10, 1_000_000, 5, 2, 2),
        redaction_key=b"a" * 32,
    )

    assert client.calls == [(2, 0), (3, 0)]
    assert keys == (("2", "C2:NEW"), ("3", "C3:NEW"))
    assert manifest.catalog_digest.startswith("hmac-sha256:")
    assert manifest.source_total_matches_rows is True


def test_catalog_total_none_does_not_hide_a_later_mismatch() -> None:
    class Client:
        def __init__(self) -> None:
            self.calls = 0

        def list_crm_deal_stage_catalog_page(
            self, *, category_id: int, start: int = 0
        ) -> CrmDealStageCatalogPage:
            del category_id, start
            self.calls += 1
            item = CrmDealStageCatalogItem("2", f"C2:{self.calls}", "process")
            if self.calls == 1:
                return CrmDealStageCatalogPage((item,), 1, None, None, None)
            return CrmDealStageCatalogPage((item,), None, 3, None, None)

    manifest, _keys = collect_current_stage_catalog(
        Client(),
        category_ids=("2",),
        limits=ProbeLimits(3, 10, 1_000_000, 5, 2, 2),
        redaction_key=b"a" * 32,
    )

    assert manifest.source_total_consistent is False
    assert manifest.source_total_matches_rows is None
