from __future__ import annotations

import math
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import src.connectors.bitrix_stage_history.probe as stage_history_probe
import src.connectors.bitrix_stage_history.spool as stage_history_spool
from src.connectors.bitrix_stage_history.models import (
    ProbeLimits,
    StageHistoryItem,
    StageHistoryPage,
)
from src.connectors.bitrix_stage_history.probe import (
    collect_stage_history_pass,
    manifests_are_identical,
)
from src.models import JsonValue


def _item(history_id: str, *, stage_id: str = "C2:NEW") -> StageHistoryItem:
    return StageHistoryItem(
        history_id=history_id,
        entity_type_id="2",
        owner_id="501",
        type_id="1",
        created_time=datetime(2026, 8, 6, 4, tzinfo=UTC),
        created_time_source="2026-08-06T04:00:00+00:00",
        category_id="2",
        stage_semantic_id="P",
        stage_id=stage_id,
        raw_payload={"ID": history_id},
    )


class _Client:
    def __init__(self, pages: list[StageHistoryPage]) -> None:
        self._pages = pages
        self.calls: list[tuple[int, dict[str, JsonValue]]] = []
        self.orders: list[str] = []

    def list_stage_history_page(
        self,
        *,
        entity_type_id: int,
        filters: dict[str, JsonValue] | None = None,
        order_direction: str = "ASC",
        start: int = -1,
    ) -> StageHistoryPage:
        assert entity_type_id == 2
        self.orders.append(order_direction)
        self.calls.append((start, dict(filters or {})))
        return self._pages[len(self.calls) - 1]


def _limits() -> ProbeLimits:
    return ProbeLimits(
        max_calls=3,
        max_rows=10,
        max_spool_bytes=1_000_000,
        max_runtime_seconds=5,
        max_passes=2,
        required_identical_passes=2,
    )


def test_collect_pass_accounts_for_duplicates_and_keeps_spool_restricted(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage(
                items=(_item("1"), _item("1"), _item("2")),
                next_start=None,
                total=3,
                operating=None,
                operating_reset_at=None,
            )
        ]
    )

    manifest, spool = collect_stage_history_pass(
        client,
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={"@OWNER_ID": ["501"]},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert client.calls == [(0, {"@OWNER_ID": ["501"]})]
    assert manifest.raw_rows == 3
    assert manifest.unique_identity_rows == 2
    assert manifest.duplicate_same_hash_rows == 1
    assert manifest.duplicate_conflict_rows == 0
    assert manifest.identity_hash_digest.startswith("sha256:")
    assert spool.path.stat().st_mode & 0o077 == 0
    spool.delete()


def test_collect_pass_counts_same_identity_different_hash_as_conflict(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage(
                items=(_item("1"), _item("1", stage_id="C2:WON")),
                next_start=None,
                total=2,
                operating=None,
                operating_reset_at=None,
            )
        ]
    )

    manifest, spool = collect_stage_history_pass(
        client,
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert manifest.duplicate_conflict_rows == 1
    spool.delete()


def test_conflict_variants_are_retained_in_manifest_digest(tmp_path: Path) -> None:
    single_manifest, single_spool = collect_stage_history_pass(
        _Client([StageHistoryPage((_item("1"),), None, 1, None, None)]),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )
    conflict_manifest, conflict_spool = collect_stage_history_pass(
        _Client(
            [
                StageHistoryPage(
                    (_item("1"), _item("1", stage_id="C2:WON")),
                    None,
                    2,
                    None,
                    None,
                )
            ]
        ),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=2,
    )

    assert conflict_manifest.duplicate_conflict_rows == 1
    assert conflict_manifest.identity_hash_digest != single_manifest.identity_hash_digest
    single_spool.delete()
    conflict_spool.delete()


def test_collect_pass_fails_closed_when_page_total_changes(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage((_item("1"),), 1, 2, None, None),
            StageHistoryPage((_item("2"),), None, 3, None, None),
        ]
    )

    manifest, spool = collect_stage_history_pass(
        client,
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert manifest.source_total_consistent is False
    assert manifest.source_total_matches_rows is None
    spool.delete()


def test_identical_manifests_require_matching_source_set(tmp_path: Path) -> None:
    page = StageHistoryPage((_item("1"),), None, 1, None, None)
    first, first_spool = collect_stage_history_pass(
        _Client([page]),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )
    second, second_spool = collect_stage_history_pass(
        _Client([page]),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=2,
    )

    assert manifests_are_identical(first, second)
    assert not manifests_are_identical(first, replace(second, calls=second.calls + 1))
    assert not manifests_are_identical(first, replace(second, pages=second.pages + 1))
    first_spool.delete()
    second_spool.delete()


def test_collect_pass_refuses_to_exceed_call_limit(tmp_path: Path) -> None:
    client = _Client([StageHistoryPage((_item("1"),), 1, 2, None, None)])
    limits = ProbeLimits(1, 10, 1_000_000, 5, 2, 2)

    with pytest.raises(RuntimeError, match="call limit"):
        collect_stage_history_pass(
            client,
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=limits,
            spool_directory=tmp_path / "restricted",
            pass_number=1,
        )

    assert list((tmp_path / "restricted").glob("*.sqlite3")) == []


def test_keyset_pass_advances_the_exclusive_id_filter(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage(
                tuple(_item(str(index)) for index in range(1, 51)),
                None,
                None,
                None,
                None,
            ),
            StageHistoryPage((_item("51"),), None, None, None, None),
        ]
    )

    manifest, spool = collect_stage_history_pass(
        client,
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={"@OWNER_ID": ["501"]},
        limits=ProbeLimits(3, 100, 1_000_000, 5, 2, 2),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
        traversal_mode="id_keyset",
    )

    assert client.calls == [
        (-1, {"@OWNER_ID": ["501"]}),
        (-1, {"@OWNER_ID": ["501"], ">ID": "50"}),
    ]
    assert manifest.traversal_mode == "id_keyset"
    assert manifest.raw_rows == 51
    spool.delete()


def test_collect_pass_detects_total_appearing_after_first_page(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage((_item("1"),), 1, None, None, None),
            StageHistoryPage((_item("2"),), None, 2, None, None),
        ]
    )

    manifest, spool = collect_stage_history_pass(
        client,
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert manifest.source_total is None
    assert manifest.source_total_consistent is False
    assert manifest.source_total_matches_rows is None
    spool.delete()


def test_keyset_pass_rejects_overlap_on_partial_terminal_page(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage(
                tuple(_item(str(index)) for index in range(1, 51)),
                None,
                None,
                None,
                None,
            ),
            StageHistoryPage((_item("50"), _item("51")), None, None, None, None),
        ]
    )

    with pytest.raises(RuntimeError, match="did not advance"):
        collect_stage_history_pass(
            client,
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=ProbeLimits(3, 100, 1_000_000, 5, 2, 2),
            spool_directory=tmp_path / "restricted",
            pass_number=1,
            traversal_mode="id_keyset",
        )


def test_keyset_pass_rejects_unordered_partial_terminal_page(tmp_path: Path) -> None:
    client = _Client(
        [
            StageHistoryPage(
                tuple(_item(str(index)) for index in range(1, 51)),
                None,
                None,
                None,
                None,
            ),
            StageHistoryPage((_item("52"), _item("51")), None, None, None, None),
        ]
    )

    with pytest.raises(RuntimeError, match="strictly increasing"):
        collect_stage_history_pass(
            client,
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=ProbeLimits(3, 100, 1_000_000, 5, 2, 2),
            spool_directory=tmp_path / "restricted",
            pass_number=1,
            traversal_mode="id_keyset",
        )


@pytest.mark.parametrize("runtime", [math.nan, math.inf, -math.inf])
def test_probe_limits_reject_non_finite_runtime(runtime: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        ProbeLimits(1, 1, 1, runtime, 2, 2)


@pytest.mark.parametrize("position", range(6))
def test_probe_limits_reject_boolean_numeric_fields(position: int) -> None:
    values: list[int | float | bool] = [1, 1, 1, 1.0, 2, 2]
    values[position] = True

    with pytest.raises(ValueError):
        ProbeLimits(*values)


@pytest.mark.parametrize("position", [0, 1, 2, 4, 5])
def test_probe_limits_reject_float_integer_fields(position: int) -> None:
    values: list[int | float] = [1, 1, 1, 1.0, 2, 2]
    values[position] = 1.5

    with pytest.raises(ValueError):
        ProbeLimits(*values)


def test_collect_pass_rejects_permissive_existing_directory(tmp_path: Path) -> None:
    restricted = tmp_path / "restricted"
    restricted.mkdir(mode=0o755)

    with pytest.raises(ValueError, match="permissions"):
        collect_stage_history_pass(
            _Client([]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=restricted,
            pass_number=1,
        )

    assert restricted.stat().st_mode & 0o077 != 0


def test_collect_pass_rejects_symlink_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    restricted = tmp_path / "restricted"
    restricted.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        collect_stage_history_pass(
            _Client([]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=restricted,
            pass_number=1,
        )


def test_spool_initialization_failure_removes_created_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    restricted = tmp_path / "restricted"

    def fail_connect(_path: Path) -> sqlite3.Connection:
        raise sqlite3.OperationalError("simulated initialization failure")

    monkeypatch.setattr(stage_history_spool.sqlite3, "connect", fail_connect)

    with pytest.raises(sqlite3.OperationalError, match="simulated"):
        collect_stage_history_pass(
            _Client([]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=restricted,
            pass_number=1,
        )

    assert list(restricted.glob("*.sqlite3")) == []


def test_invalid_traversal_mode_does_not_create_spool_directory(tmp_path: Path) -> None:
    restricted = tmp_path / "restricted"

    with pytest.raises(ValueError, match="unsupported"):
        collect_stage_history_pass(
            _Client([]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=restricted,
            pass_number=1,
            traversal_mode="invalid",
        )

    assert not restricted.exists()


def test_offset_pass_reconciles_total_and_uses_numeric_id_bounds(tmp_path: Path) -> None:
    manifest, spool = collect_stage_history_pass(
        _Client([StageHistoryPage((_item("9"), _item("10")), None, 2, None, None)]),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )

    assert manifest.source_total_matches_rows is True
    assert manifest.history_id_ordering == "numeric"
    assert manifest.minimum_history_id == "9"
    assert manifest.maximum_history_id == "10"
    assert "minimum_history_id" not in manifest.to_dict()
    assert "maximum_history_id" not in manifest.to_dict()
    assert manifest.to_dict()["history_id_bounds_redacted"] is True
    spool.delete()


def test_total_row_mismatch_prevents_convergence(tmp_path: Path) -> None:
    page = StageHistoryPage((_item("1"),), None, 2, None, None)
    first, first_spool = collect_stage_history_pass(
        _Client([page]),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=1,
    )
    second, second_spool = collect_stage_history_pass(
        _Client([page]),
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=2,
    )

    assert first.source_total_matches_rows is False
    assert second.source_total_matches_rows is False
    assert not manifests_are_identical(first, second)
    first_spool.delete()
    second_spool.delete()


def test_spool_limit_counts_sqlite_sidecar_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_add = stage_history_probe.RestrictedSpool.add

    def add_with_large_sidecar(
        spool: stage_history_probe.RestrictedSpool,
        stable_id: str,
        canonical_hash: str,
    ) -> str:
        disposition = original_add(spool, stable_id, canonical_hash)
        Path(f"{spool.path}-wal").write_bytes(b"x" * 200_000)
        return disposition

    monkeypatch.setattr(stage_history_probe.RestrictedSpool, "add", add_with_large_sidecar)

    with pytest.raises(RuntimeError, match="spool limit"):
        collect_stage_history_pass(
            _Client([StageHistoryPage((_item("1"),), None, 1, None, None)]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=ProbeLimits(3, 10, 100_000, 5, 2, 2),
            spool_directory=tmp_path / "restricted",
            pass_number=1,
        )

    assert list((tmp_path / "restricted").iterdir()) == []


def test_runtime_limit_covers_manifest_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    original_digest = stage_history_probe.RestrictedSpool.manifest_digest

    def delayed_digest(spool: stage_history_probe.RestrictedSpool) -> str:
        digest = original_digest(spool)
        clock[0] = 10.0
        return digest

    monkeypatch.setattr(stage_history_probe.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        stage_history_probe.RestrictedSpool,
        "manifest_digest",
        delayed_digest,
    )

    with pytest.raises(RuntimeError, match="runtime limit"):
        collect_stage_history_pass(
            _Client([StageHistoryPage((_item("1"),), None, 1, None, None)]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=tmp_path / "restricted",
            pass_number=1,
        )

    assert list((tmp_path / "restricted").iterdir()) == []


def test_reconciliation_spool_indexes_owner_lookups(tmp_path: Path) -> None:
    from src.connectors.bitrix_stage_history.reconciliation_spool import (
        CapabilityReconciliationSpool,
    )

    spool = CapabilityReconciliationSpool(tmp_path / "restricted", 1)
    connection = sqlite3.connect(spool.path)
    try:
        indexes = {row[1] for row in connection.execute("PRAGMA index_list('events')")}
        assert "events_owner_id_idx" in indexes
        plan = " ".join(
            str(part)
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM events WHERE owner_id = ? LIMIT 1",
                ("501",),
            )
            for part in row
        )
        assert "events_owner_id_idx" in plan
    finally:
        connection.close()
        spool.delete()


def test_global_frozen_stage_pass_reconciles_against_restricted_owner_manifest(
    tmp_path: Path,
) -> None:
    from src.connectors.bitrix_openlines.models import CrmDealCapabilityItem
    from src.connectors.bitrix_stage_history.deal_probe import RestrictedOwnerManifest
    from src.connectors.bitrix_stage_history.probe import freeze_stage_history_upper_id

    owner_manifest = RestrictedOwnerManifest(tmp_path / "restricted", 1)
    owner_manifest.add(CrmDealCapabilityItem("501", "2", "C2:NEW"))
    owner_manifest.flush()
    redaction_key = b"a" * 32
    owner_digest = owner_manifest.manifest_digest(redaction_key=redaction_key)
    boundary_client = _Client([StageHistoryPage((_item("10"), _item("9")), None, None, None, None)])
    assert freeze_stage_history_upper_id(boundary_client, 2) == 10
    assert boundary_client.calls == [(-1, {})]
    assert boundary_client.orders == ["DESC"]

    client = _Client(
        [
            StageHistoryPage(
                (_item("1"), replace(_item("2"), owner_id="999")),
                None,
                2,
                None,
                None,
            )
        ]
    )
    manifest, spool = collect_stage_history_pass(
        client,
        source_contract_id="123e4567-e89b-12d3-a456-426614174000",
        entity_type_id=2,
        filters={},
        limits=_limits(),
        spool_directory=tmp_path / "restricted",
        pass_number=2,
        traversal_mode="id_keyset",
        upper_history_id=10,
        owner_manifest_path=owner_manifest.path,
        owner_manifest_digest=owner_digest,
        redaction_key=redaction_key,
        current_catalog_stage_keys=(("2", "C2:NEW"),),
    )

    assert client.calls == [(-1, {"<=ID": "10"})]
    assert client.orders == ["ASC"]
    assert manifest.upper_history_id_digest is not None
    assert manifest.owner_manifest_digest == owner_digest
    assert manifest.upper_history_id_digest.startswith("hmac-sha256:")
    assert manifest.identity_hash_digest.startswith("hmac-sha256:")
    assert manifest.global_rows == 2
    assert manifest.in_scope_rows == 1
    assert manifest.out_of_scope_rows == 1
    assert manifest.owners_without_history == 0
    assert manifest.in_scope_identity_hash_digest is not None
    assert manifest.current_catalog_stage_count == 1
    assert manifest.in_scope_historical_stage_count == 1
    assert manifest.in_scope_historical_stage_missing_catalog_count == 0
    assert manifest.in_scope_rows_missing_stage_identity == 0
    spool.delete()
    owner_manifest.delete()


def test_stage_boundary_rejects_duplicate_descending_ids() -> None:
    from src.connectors.bitrix_stage_history.probe import freeze_stage_history_upper_id

    client = _Client([StageHistoryPage((_item("10"), _item("10")), None, None, None, None)])

    with pytest.raises(RuntimeError, match="not descending"):
        freeze_stage_history_upper_id(client, 2)


def test_global_reconciliation_rejects_an_owner_manifest_digest_from_another_run(
    tmp_path: Path,
) -> None:
    from src.connectors.bitrix_openlines.models import CrmDealCapabilityItem
    from src.connectors.bitrix_stage_history.deal_probe import RestrictedOwnerManifest

    owner_manifest = RestrictedOwnerManifest(tmp_path / "restricted", 1)
    owner_manifest.add(CrmDealCapabilityItem("501", "2", "C2:NEW"))
    owner_manifest.flush()

    with pytest.raises(RuntimeError, match="owner manifest digest"):
        collect_stage_history_pass(
            _Client([StageHistoryPage((_item("1"),), None, 1, None, None)]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=tmp_path / "restricted",
            pass_number=2,
            traversal_mode="id_keyset",
            upper_history_id=10,
            owner_manifest_path=owner_manifest.path,
            owner_manifest_digest=owner_manifest.manifest_digest(redaction_key=b"a" * 32),
            redaction_key=b"b" * 32,
        )

    owner_manifest.delete()


def test_global_frozen_stage_pass_rejects_source_rows_above_boundary(tmp_path: Path) -> None:
    from src.connectors.bitrix_openlines.models import CrmDealCapabilityItem
    from src.connectors.bitrix_stage_history.deal_probe import RestrictedOwnerManifest

    owner_manifest = RestrictedOwnerManifest(tmp_path / "restricted", 1)
    owner_manifest.add(CrmDealCapabilityItem("501", "2", "C2:NEW"))
    owner_manifest.flush()

    with pytest.raises(RuntimeError, match="frozen upper boundary"):
        collect_stage_history_pass(
            _Client([StageHistoryPage((_item("11"),), None, 1, None, None)]),
            source_contract_id="123e4567-e89b-12d3-a456-426614174000",
            entity_type_id=2,
            filters={},
            limits=_limits(),
            spool_directory=tmp_path / "restricted",
            pass_number=2,
            traversal_mode="id_keyset",
            upper_history_id=10,
            owner_manifest_path=owner_manifest.path,
            owner_manifest_digest=owner_manifest.manifest_digest(),
        )

    owner_manifest.delete()
