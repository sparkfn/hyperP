"""Regression coverage for #422's bounded canonical status snapshot."""

from __future__ import annotations

import shutil
import time
import tracemalloc
from collections.abc import Iterator
from pathlib import Path

try:
    import resource
except ImportError:  # pragma: no cover - Windows CI does not expose getrusage.
    resource = None

import pytest

from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair import bounded
from src.crm_deal_identity_repair.digests import (
    inventory_digest,
    inventory_digest_from_parts,
    object_digest,
)
from src.crm_deal_identity_repair.models import RepairInventoryItem
from src.graph import crm_deal_identity_repair_status_snapshot as status_snapshot
from src.graph.crm_deal_identity_repair_boundary_evidence import (
    canonical_boundary_evidence,
    canonical_evidence_rows,
)
from src.graph.crm_deal_identity_repair_ledger import _control_digest_value
from src.graph.queries.crm_deal_identity_repair import (
    INVENTORY_ACTIVE_CRM_DEALS,
    INVENTORY_CRM_DEAL_PROJECTIONS_PAGE,
    INVENTORY_INVALID_CRM_DEAL_SOURCE_RECORD_PKS,
    INVENTORY_STALE_RUN_CONTROL_PLANE,
)
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


class _GuardedResult:
    def __init__(
        self,
        transaction: _GuardedStatusTransaction,
        rows: Iterator[dict[str, JsonValue]],
    ) -> None:
        self._transaction = transaction
        self._rows = rows
        self._exhausted = False
        self._consumed = False
        self._row_count = 0

    def __iter__(self) -> Iterator[dict[str, JsonValue]]:
        if self._consumed:
            raise AssertionError("status attempted to iterate an already-consumed result")
        for row in self._rows:
            self._row_count += 1
            self._transaction.max_rows_per_result = max(
                self._transaction.max_rows_per_result,
                self._row_count,
            )
            yield row
        self._exhausted = True

    def consume(self) -> None:
        if not self._exhausted:
            raise AssertionError("status must exhaust every result before consume")
        self._consumed = True
        self._transaction.live_result = None

    def data(self) -> list[dict[str, JsonValue]]:
        raise AssertionError("status must not materialize result.data()")


class _SmallResult:
    def __init__(self, rows: tuple[dict[str, JsonValue], ...]) -> None:
        self._rows = rows
        self.consumed = False

    def __iter__(self) -> Iterator[dict[str, JsonValue]]:
        return iter(self._rows)

    def consume(self) -> None:
        self.consumed = True


class _GuardedStatusTransaction:
    def __init__(self, total: int, negative_control_indices: frozenset[int]) -> None:
        self.total = total
        self.negative_control_indices = negative_control_indices
        self.live_result: _GuardedResult | None = None
        self.max_live_results = 0
        self.max_active_page_size = 0
        self.max_projection_param_size = 0
        self.max_source_param_size = 0
        self.max_rows_per_result = 0
        self._payload_blob = "x" * 512

    def run(self, query: str, **parameters: object) -> _GuardedResult:
        if self.live_result is not None:
            raise AssertionError("status opened a nested query before consuming the prior result")
        rows = self._rows(query, parameters)
        result = _GuardedResult(self, rows)
        self.live_result = result
        self.max_live_results = max(self.max_live_results, 1)
        return result

    def _rows(
        self,
        query: str,
        parameters: dict[str, object],
    ) -> Iterator[dict[str, JsonValue]]:
        if query == INVENTORY_INVALID_CRM_DEAL_SOURCE_RECORD_PKS:
            yield {"invalid_source_record_pk_count": 0}
            return
        if query == INVENTORY_ACTIVE_CRM_DEALS:
            yield from self._active_page(parameters)
            return
        if query == INVENTORY_CRM_DEAL_PROJECTIONS_PAGE:
            yield from self._projection_page(parameters)
            return
        if query == INVENTORY_STALE_RUN_CONTROL_PLANE:
            yield {
                "stale_run_id": _STALE_RUN_ID,
                "stale_run_state": "unknown",
                "run_status": None,
                "associated_source_system": None,
                "logical_run_association_count": 0,
                "checkpoint_association_count": 0,
            }
            return
        if query == READ_SOURCE_RECORD_BOUNDARY:
            yield from self._source_page(parameters)
            return
        if query == READ_STALE_RUN_CONTROL_EVIDENCE:
            yield self._stale_evidence_row()
            return
        if query == READ_STALE_RUN_ASSOCIATIONS:
            return
        if query == READ_INSTANCE_CONTROL_BOUNDARY:
            yield self._control_row()
            return
        if query == READ_CONTROL_DISPATCH_EVIDENCE:
            yield {"labels": ["BitrixDispatchControl"], "properties": {"blocked": False}}
            return
        if query == READ_CONTROL_NODES:
            yield {"labels": ["IngestionLogicalRun"], "properties": {"ordered": ["a", "b"]}}
            yield {"labels": ["IngestionLogicalRun"], "properties": {"ordered": ["a", "b"]}}
            return
        if query == READ_CONTROL_RELATIONSHIPS:
            yield self._relationship_evidence_row()
            return
        raise AssertionError("unexpected status query")

    def _active_page(self, parameters: dict[str, object]) -> Iterator[dict[str, JsonValue]]:
        after = parameters.get("after_source_record_pk")
        limit = parameters.get("limit")
        if not isinstance(after, str) or limit != 100:
            raise AssertionError("status active inventory page parameters are invalid")
        start = 0 if after == "" else int(after.removeprefix("pk-")) + 1
        stop = min(start + 100, self.total)
        self.max_active_page_size = max(self.max_active_page_size, stop - start)
        for index in range(start, stop):
            yield self._inventory_record(index)

    def _projection_page(self, parameters: dict[str, object]) -> Iterator[dict[str, JsonValue]]:
        raw_pks = parameters.get("source_record_pks")
        if not isinstance(raw_pks, list) or len(raw_pks) > 100:
            raise AssertionError("status projection query received an unbounded PK parameter")
        if not all(isinstance(pk, str) for pk in raw_pks):
            raise AssertionError("status projection PK parameter is malformed")
        self.max_projection_param_size = max(self.max_projection_param_size, len(raw_pks))
        for source_record_pk in raw_pks:
            index = int(source_record_pk.removeprefix("pk-"))
            if index not in self.negative_control_indices:
                continue
            yield {
                "source_record_pk": source_record_pk,
                "projection": {
                    "relationship_type": "IDENTIFIED_BY",
                    "is_active": True,
                    "relationship_properties": {"provenance": "fixture"},
                    "owner_person_id": f"person-{index}",
                    "identifier_type": "crm_contact_id",
                    "identifier_value": f"contact-{index}",
                    "address_id": None,
                    "target_source_record_pk": None,
                    "source_record_pk": source_record_pk,
                },
            }

    def _source_page(self, parameters: dict[str, object]) -> Iterator[dict[str, JsonValue]]:
        raw_pks = parameters.get("source_record_pks")
        if not isinstance(raw_pks, list) or len(raw_pks) > 100:
            raise AssertionError("status source query received an unbounded PK parameter")
        if not all(isinstance(pk, str) for pk in raw_pks):
            raise AssertionError("status source PK parameter is malformed")
        self.max_source_param_size = max(self.max_source_param_size, len(raw_pks))
        for source_record_pk in raw_pks:
            index = int(source_record_pk.removeprefix("pk-"))
            yield {
                "source_record_pk": source_record_pk,
                "source_record_id": f"bitrix-crm-deal-{index:06d}",
                "source_record_version": "1",
                "source_version_key": f"{source_record_pk}:1",
                "record_hash": f"hash-{index}",
                "lifecycle_status": "active",
                "is_latest": True,
                "source_instance_id": "portal-a",
            }

    def _inventory_record(self, index: int) -> dict[str, JsonValue]:
        source_record_pk = f"pk-{index:06d}"
        current_version = "2" if index == 71_332 else "1"
        owner_ids: list[JsonValue]
        if index in self.negative_control_indices:
            owner_ids = [{"person_id": f"person-{index}", "is_active": True}]
        else:
            owner_ids = [
                {"person_id": f"person-{index}-a", "is_active": True},
                {"person_id": f"person-{index}-b", "is_active": True},
            ]
        logical_versions: list[JsonValue] = [
            {
                "source_record_pk": source_record_pk,
                "source_record_version": current_version,
                "lifecycle_status": "active",
                "is_latest": True,
                "raw_payload": self._payload("Ångström"),
                "normalized_payload": "{}",
            }
        ]
        if index == 71_332:
            logical_versions.insert(
                0,
                {
                    "source_record_pk": f"history-{index}",
                    "source_record_version": "1",
                    "lifecycle_status": "superseded",
                    "is_latest": False,
                    "raw_payload": self._payload("historical"),
                    "normalized_payload": "{}",
                },
            )
        return {
            "source_record_pk": source_record_pk,
            "source_record_id": f"bitrix-crm-deal-{index:06d}",
            "source_record_version": current_version,
            "lifecycle_status": "active",
            "is_latest": True,
            "record_hash": f"hash-{index}",
            "observed_at": "2026-09-12T00:00:00Z",
            "raw_payload": self._payload("Ångström"),
            "normalized_payload": "{}",
            "linked_people": owner_ids,
            "logical_versions": logical_versions,
            "descendants": [],
            "decisions_and_reviews": [],
            "owner_impacts": [],
        }

    def _stale_evidence_row(self) -> dict[str, JsonValue]:
        return {
            "stale_run_state": "absent",
            "left_labels": [],
            "left_properties": None,
            "relationship_type": None,
            "relationship_properties": None,
            "right_labels": [],
            "right_properties": None,
        }

    def _payload(self, marker: str) -> str:
        return (
            '{"crm_deal_identity_policy_version":"legacy","marker":"'
            + marker
            + '","blob":"'
            + self._payload_blob
            + '"}'
        )

    def _control_row(self) -> dict[str, JsonValue]:
        return {
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

    def _relationship_evidence_row(self) -> dict[str, JsonValue]:
        return {
            "left_labels": ["IngestionLogicalRun"],
            "left_properties": {"ordered": ["a", "b"]},
            "relationship_type": "HAS_ATTEMPT",
            "relationship_properties": {"ordered": ["first", "second"]},
            "right_labels": ["IngestRun"],
            "right_properties": {"status": "failed"},
        }


def test_status_snapshot_streams_full_high_cardinality_boundary() -> None:
    total = 178_328
    expected_pks = tuple(f"pk-{index:06d}" for index in range(total))
    negative_control_indices = frozenset({0, 35_665, 71_331, 106_997, 142_663, 178_327})
    transaction = _GuardedStatusTransaction(total, negative_control_indices)

    started = time.perf_counter()
    tracemalloc.start()
    try:
        boundary = status_snapshot.status_snapshot_from_transaction(
            transaction,
            "portal-a",
            "portal-a",
            expected_pks,
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    elapsed_seconds = time.perf_counter() - started
    rss_after = _maximum_rss_bytes()

    assert boundary.inventory_row_count == total
    assert boundary.negative_control_count == 6
    assert boundary.eligible_unit_count == 178_322
    assert boundary.inventory_digest.startswith("sha256:")
    assert boundary.source_records_digest.startswith("sha256:")
    assert boundary.stale_run_evidence_digest.startswith("sha256:")
    assert boundary.control_digest.startswith("sha256:")
    assert boundary.boundary_digest.startswith("sha256:")
    assert transaction.live_result is None
    assert transaction.max_live_results == 1
    assert transaction.max_active_page_size == 100
    assert transaction.max_projection_param_size == 100
    assert transaction.max_source_param_size == 100
    assert transaction.max_rows_per_result == 100
    assert peak_bytes < 48 * 1024 * 1024
    assert elapsed_seconds > 0
    if resource is not None:
        assert rss_after < 2 * 1024 * 1024 * 1024


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

        def run(self, query: str, **parameters: object) -> _SmallResult:
            assert query == READ_SOURCE_RECORD_BOUNDARY
            requested = parameters["source_record_pks"]
            assert isinstance(requested, list)
            self.page_sizes.append(len(requested))
            return _SmallResult(tuple(rows[pk] for pk in requested if isinstance(pk, str)))

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
    payload: dict[str, JsonValue] = {
        "linked_people": [],
        "projections": [],
        "logical_version_evidence": {"anomaly_codes": []},
        "lifecycle_policy_evidence": {"disposition": "preserve"},
        "descendants": [],
        "decisions_and_reviews": [],
        "owner_impacts": [],
    }
    items = (
        RepairInventoryItem(
            source_system="bitrix_chat",
            source_record_id="bitrix-crm-deal-zeta",
            source_record_pk="pk-a",
            deal_id="zeta",
            partition="negative_control",
            graph_fingerprint="sha256:" + "a" * 64,
            stored_payload_fingerprint="sha256:" + "b" * 64,
            payload=payload,
        ),
        RepairInventoryItem(
            source_system="bitrix_chat",
            source_record_id="bitrix-crm-deal-äther",
            source_record_pk="pk-z",
            deal_id="äther",
            partition="negative_control",
            graph_fingerprint="sha256:" + "c" * 64,
            stored_payload_fingerprint="sha256:" + "d" * 64,
            payload=payload,
        ),
        RepairInventoryItem(
            source_system="bitrix_chat",
            source_record_id="bitrix-crm-deal-alpha",
            source_record_pk="pk-b",
            deal_id="alpha",
            partition="negative_control",
            graph_fingerprint="sha256:" + "e" * 64,
            stored_payload_fingerprint="sha256:" + "f" * 64,
            payload=payload,
        ),
    )
    with bounded.CanonicalByteSorter(unique_keys=True) as sorter:
        for item in items:
            sorter.add(item.inventory_key.encode("utf-8"), canonical_json_bytes(item.to_dict()))
        disk_sorted_digest = inventory_digest_from_parts(sorter.values())
    assert disk_sorted_digest == inventory_digest(items)


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
        def run(self, query: str, **_parameters: object) -> _SmallResult:
            if query == READ_STALE_RUN_CONTROL_EVIDENCE:
                return _SmallResult((duplicate_row, duplicate_row))
            if query == READ_STALE_RUN_ASSOCIATIONS:
                return _SmallResult((association, association))
            if query == READ_INSTANCE_CONTROL_BOUNDARY:
                return _SmallResult((control_row,))
            if query == READ_CONTROL_DISPATCH_EVIDENCE:
                return _SmallResult((duplicate_row,))
            if query == READ_CONTROL_NODES:
                return _SmallResult((duplicate_row, duplicate_row))
            if query == READ_CONTROL_RELATIONSHIPS:
                return _SmallResult((association, association))
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
    normalized_control["dispatch_evidence"] = canonical_evidence_rows((duplicate_row,))
    normalized_control["control_nodes"] = canonical_evidence_rows(
        (duplicate_row, duplicate_row)
    )
    normalized_control["control_relationships"] = canonical_evidence_rows(
        (association, association)
    )
    instance_expected = object_digest(
        b"crm-deal-identity-repair-source-instance-boundary-v1\x00",
        status_snapshot._instance_digest_value(normalized_control),
    )
    control_expected = object_digest(
        b"crm-deal-identity-repair-control-boundary-v1\x00",
        _control_digest_value(normalized_control),
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
