"""Regression coverage for #422's bounded canonical status snapshot."""

from __future__ import annotations

import inspect
import shutil
import time
import tracemalloc
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - Windows CI does not expose getrusage.
    resource = None

import pytest

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair import bounded
from src.crm_deal_identity_repair.digests import inventory_digest_from_parts, object_digest
from src.graph import crm_deal_identity_repair_status_snapshot as status_snapshot
from src.graph.crm_deal_identity_repair_boundary_evidence import (
    canonical_boundary_evidence,
    canonical_evidence_rows,
)
from src.graph.crm_deal_identity_repair_ledger import _control_digest_value
from src.graph.queries.crm_deal_identity_repair import INVENTORY_STALE_RUN_CONTROL_PLANE
from src.graph.queries.crm_deal_identity_repair_ledger import (
    READ_CONTROL_DISPATCH_EVIDENCE,
    READ_CONTROL_NODES,
    READ_CONTROL_RELATIONSHIPS,
    READ_INSTANCE_CONTROL_BOUNDARY,
    READ_SOURCE_RECORD_BOUNDARY,
    READ_STALE_RUN_ASSOCIATIONS,
    READ_STALE_RUN_CONTROL_EVIDENCE,
)
from src.models import JsonValue


_STALE_RUN_ID = "e5deb1d6-7333-4660-be4f-c44fcf5af686"


class _Result:
    def __init__(self, rows: tuple[dict[str, JsonValue], ...]) -> None:
        self._rows = rows
        self.consumed = False

    def __iter__(self) -> Iterator[dict[str, JsonValue]]:
        return iter(self._rows)

    def consume(self) -> None:
        self.consumed = True


class _InventoryTransaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        if query == INVENTORY_STALE_RUN_CONTROL_PLANE:
            return _Result(
                (
                    {
                        "stale_run_id": _STALE_RUN_ID,
                        "stale_run_state": "unknown",
                        "run_status": None,
                        "associated_source_system": None,
                        "logical_run_association_count": 0,
                        "checkpoint_association_count": 0,
                    },
                )
            )
        if query == READ_STALE_RUN_CONTROL_EVIDENCE:
            return _Result(
                (
                    {
                        "stale_run_state": "absent",
                        "left_labels": [],
                        "left_properties": None,
                        "relationship_type": None,
                        "relationship_properties": None,
                        "right_labels": [],
                        "right_properties": None,
                    },
                )
            )
        if query == READ_STALE_RUN_ASSOCIATIONS:
            return _Result(())
        raise AssertionError("unexpected query")


@dataclass(frozen=True)
class _InventoryItem:
    source_record_id: str
    source_record_pk: str
    negative_control: bool

    @property
    def inventory_key(self) -> str:
        return "|".join(("bitrix_chat", self.source_record_id, self.source_record_pk))

    @property
    def partition(self) -> str:
        return "negative_control" if self.negative_control else "ownership_repair"

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source_system": "bitrix_chat",
            "source_record_id": self.source_record_id,
            "source_record_pk": self.source_record_pk,
            "partition": self.partition,
            "payload": {"unicode": "Ångström", "payload": "x" * 512},
            "execution_allowed": False,
        }


def test_status_inventory_streams_large_boundary_without_global_projection_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    total = 178_328
    expected_pks = tuple(f"pk-{index:06d}" for index in range(total))
    negative_control_indices = frozenset({0, 35_665, 71_331, 106_997, 142_663, 178_327})
    transaction = _InventoryTransaction()
    observed_page_sizes: list[int] = []

    def pages(*_args: object, **_kwargs: object) -> Iterator[tuple[dict[str, JsonValue], ...]]:
        for start in range(0, total, 100):
            stop = min(start + 100, total)
            yield tuple(
                {
                    "source_record_pk": f"pk-{index:06d}",
                    "source_record_id": f"deal-{index:06d}",
                }
                for index in range(start, stop)
            )

    def projections(
        _tx: object,
        *,
        source_system: str,
        source_record_pks: tuple[str, ...],
    ) -> dict[str, list[JsonValue]]:
        assert source_system == "bitrix_chat"
        assert len(source_record_pks) <= 100
        observed_page_sizes.append(len(source_record_pks))
        return {}

    def item(record: dict[str, JsonValue], _rows: list[JsonValue]) -> _InventoryItem:
        source_record_id = record["source_record_id"]
        source_record_pk = record["source_record_pk"]
        assert isinstance(source_record_id, str)
        assert isinstance(source_record_pk, str)
        return _InventoryItem(
            source_record_id,
            source_record_pk,
            int(source_record_pk.removeprefix("pk-")) in negative_control_indices,
        )

    monkeypatch.setattr(status_snapshot, "validate_repair_inventory_keys", lambda *_args: None)
    monkeypatch.setattr(status_snapshot, "iter_repair_inventory_pages", pages)
    monkeypatch.setattr(status_snapshot, "page_projection_rows", projections)
    monkeypatch.setattr(status_snapshot, "page_inventory_item", item)

    started = time.perf_counter()
    tracemalloc.start()
    try:
        boundary = status_snapshot._current_inventory_boundary(transaction, expected_pks)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed_seconds = time.perf_counter() - started
    rss_after = _maximum_rss_bytes()

    assert boundary.inventory_row_count == total
    assert boundary.negative_control_count == 6
    assert boundary.eligible_unit_count == 178_322
    assert observed_page_sizes and max(observed_page_sizes) == 100
    assert len(observed_page_sizes) == 1_784
    assert peak_bytes < 48 * 1024 * 1024
    assert elapsed_seconds > 0
    if resource is not None:
        assert rss_after < 2 * 1024 * 1024 * 1024
    assert "items.extend" not in inspect.getsource(status_snapshot)
    assert "projections_by_pk" not in inspect.getsource(status_snapshot)


def test_source_record_digest_pages_exact_legacy_object_bytes() -> None:
    source_record_pks = ("pk-a", "pk-b", "pk-c")
    rows = {
        "pk-a": {
            "source_record_pk": "pk-a",
            "source_record_id": "ä",
            "source_record_version": "1",
            "source_version_key": "a:1",
            "record_hash": "a",
            "lifecycle_status": "active",
            "is_latest": True,
            "source_instance_id": "portal-a",
        },
        "pk-b": {
            "source_record_pk": "pk-b",
            "source_record_id": "b",
            "source_record_version": "2",
            "source_version_key": "b:2",
            "record_hash": "b",
            "lifecycle_status": None,
            "is_latest": True,
            "source_instance_id": "portal-a",
        },
        "pk-c": {
            "source_record_pk": "pk-c",
            "source_record_id": "c",
            "source_record_version": "3",
            "source_version_key": "c:3",
            "record_hash": "c",
            "lifecycle_status": "active",
            "is_latest": True,
            "source_instance_id": "portal-a",
        },
    }

    class _SourceTransaction:
        def __init__(self) -> None:
            self.page_sizes: list[int] = []

        def run(self, query: str, **parameters: object) -> _Result:
            assert query == READ_SOURCE_RECORD_BOUNDARY
            requested = parameters["source_record_pks"]
            assert isinstance(requested, list)
            self.page_sizes.append(len(requested))
            return _Result(tuple(rows[pk] for pk in requested if isinstance(pk, str)))

    transaction = _SourceTransaction()
    digest = status_snapshot._source_records_digest(
        transaction,
        source_record_pks,
        "portal-a",
    )

    expected_rows = [rows[pk] for pk in source_record_pks]
    assert digest == object_digest(
        b"crm-deal-identity-repair-source-record-boundary-v1\x00",
        {"rows": expected_rows},
    )
    assert transaction.page_sizes == [3]


def test_incremental_inventory_digest_matches_legacy_inventory_key_order() -> None:
    rows = (
        _InventoryItem("z-record", "pk-a", False),
        _InventoryItem("ä-record", "pk-z", True),
        _InventoryItem("a-record", "pk-b", False),
    )
    with status_snapshot.CanonicalByteSorter(unique_keys=True) as sorter:
        for row in rows:
            sorter.add(row.inventory_key.encode("utf-8"), canonical_json_bytes(row.to_dict()))
        observed = inventory_digest_from_parts(sorter.values())
    expected = inventory_digest_from_parts(
        canonical_json_bytes(row.to_dict())
        for row in sorted(rows, key=lambda item: item.inventory_key)
    )
    assert observed == expected


def test_incremental_stale_and_control_digests_match_legacy_canonical_objects() -> None:
    duplicate_row = {
        "labels": ["Control", "StageHistoryUnit"],
        "properties": {"ordered_checkpoints": ["first", "second"]},
    }
    association = {
        "association_kind": "logical_attempt",
        "left_labels": ["IngestionLogicalRun"],
        "left_properties": {"ordered": ["one", "two"]},
        "relationship_type": "HAS_ATTEMPT",
        "relationship_properties": {"ordered": ["left", "right"]},
        "right_labels": ["IngestRun"],
        "right_properties": {"status": "failed"},
    }
    inventory_evidence: dict[str, JsonValue] = {
        "stale_run_id": _STALE_RUN_ID,
        "state": "unknown",
        "disposition": "investigate",
        "run_status": None,
        "associated_source_system": None,
        "logical_run_association_count": 0,
        "checkpoint_association_count": 0,
        "execution_allowed": False,
    }
    control_row: dict[str, JsonValue] = {
        "source_registration_count": 1,
        "source_instance_of_count": 1,
        "source_active_instance_of_count": 1,
        "source_statuses": ["active"],
        "control_registration_count": 1,
        "control_instance_of_count": 1,
        "control_active_instance_of_count": 1,
        "control_statuses": ["active"],
        "binding_count": 1,
        "binding_ownership_count": 1,
        "requested_binding_count": 1,
        "requested_ownership_count": 1,
        "binding_source_instance_ids": ["portal-a"],
        "binding_owner_instance_ids": ["portal-a"],
        "owned_binding_source_instance_ids": ["portal-a"],
    }

    class _EvidenceTransaction:
        def run(self, query: str, **_parameters: object) -> _Result:
            if query == READ_STALE_RUN_CONTROL_EVIDENCE:
                return _Result((duplicate_row, duplicate_row))
            if query == READ_STALE_RUN_ASSOCIATIONS:
                return _Result((association, association))
            if query == READ_INSTANCE_CONTROL_BOUNDARY:
                return _Result((control_row,))
            if query == READ_CONTROL_DISPATCH_EVIDENCE:
                return _Result((duplicate_row,))
            if query == READ_CONTROL_NODES:
                return _Result((duplicate_row, duplicate_row))
            if query == READ_CONTROL_RELATIONSHIPS:
                return _Result((association, association))
            raise AssertionError("unexpected evidence query")

    transaction = _EvidenceTransaction()
    stale_observed = status_snapshot._stale_run_evidence_digest(transaction, inventory_evidence)
    stale_expected = object_digest(
        b"crm-deal-identity-repair-stale-run-boundary-v1\x00",
        {
            "inventory_evidence": canonical_boundary_evidence(inventory_evidence),
            "persisted_run": canonical_evidence_rows((duplicate_row, duplicate_row)),
            "persisted_associations": canonical_evidence_rows((association, association)),
        },
    )
    assert stale_observed == stale_expected
    instance_observed, control_observed = status_snapshot._control_digests(
        transaction,
        "portal-a",
        "portal-a",
    )
    normalized_control = canonical_boundary_evidence(control_row)
    assert isinstance(normalized_control, dict)
    normalized_control["dispatch_count"] = 1
    instance_expected = object_digest(
        b"crm-deal-identity-repair-source-instance-boundary-v1\x00",
        status_snapshot._instance_digest_value(normalized_control),
    )
    control_expected = object_digest(
        b"crm-deal-identity-repair-control-boundary-v1\x00",
        {
            **_control_digest_value(normalized_control),
            "dispatch_evidence": canonical_evidence_rows((duplicate_row,)),
            "control_nodes": canonical_evidence_rows((duplicate_row, duplicate_row)),
            "control_relationships": canonical_evidence_rows((association, association)),
        },
    )
    assert instance_observed == instance_expected
    assert control_observed == control_expected


def test_canonical_scratch_is_removed_after_snapshot_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "private-scratch"

    class _TemporaryDirectory:
        def __init__(self, *, prefix: str) -> None:
            assert prefix == "hyperp-crm-repair-status-"
            scratch.mkdir()
            self.name = str(scratch)

        def cleanup(self) -> None:
            shutil.rmtree(scratch)

    monkeypatch.setattr(bounded.tempfile, "TemporaryDirectory", _TemporaryDirectory)
    with pytest.raises(RuntimeError, match="injected failure"):
        with bounded.CanonicalByteSorter() as sorter:
            sorter.add(b"key", b"{\"value\":1}\n")
            raise RuntimeError("injected failure")
    assert not scratch.exists()


def test_canonical_scratch_is_removed_when_sqlite_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "private-setup-scratch"

    class _TemporaryDirectory:
        def __init__(self, *, prefix: str) -> None:
            assert prefix == "hyperp-crm-repair-status-"
            scratch.mkdir()
            self.name = str(scratch)

        def cleanup(self) -> None:
            shutil.rmtree(scratch)

    def fail_connect(_database: object) -> object:
        raise OSError("injected SQLite setup failure")

    monkeypatch.setattr(bounded.tempfile, "TemporaryDirectory", _TemporaryDirectory)
    monkeypatch.setattr(bounded.sqlite3, "connect", fail_connect)
    with pytest.raises(OSError, match="injected SQLite setup failure"):
        bounded.CanonicalByteSorter().__enter__()
    assert not scratch.exists()


def _maximum_rss_bytes() -> int:
    if resource is None:
        return 0
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
