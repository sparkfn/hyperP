"""Regression coverage for #422's bounded canonical status snapshot."""

from __future__ import annotations

import shutil
import sys
import time
import tracemalloc
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest

from _crm_deal_identity_repair_probe import (
    ProbeMetric,
    child_probe_requested,
    emit_child_probe_result,
    format_probe_evidence,
    format_probe_metrics,
    run_child_probe,
)
from src.connectors.bitrix_stage_history.artifact_manifest import canonical_json_bytes
from src.crm_deal_identity_repair import bounded
from src.crm_deal_identity_repair.digests import (
    inventory_digest,
    inventory_digest_from_parts,
    object_digest,
)
from src.crm_deal_identity_repair.execution_boundary_models import RepairBoundarySnapshot
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

_FULL_STATUS_WORKLOAD: Final = "status-snapshot-full-178328"
_FULL_STATUS_TOTAL: Final = 178_328
_FULL_STATUS_NEGATIVE_CONTROL_INDICES: Final = frozenset(
    {0, 35_665, 71_331, 106_997, 142_663, 178_327}
)
_FULL_STATUS_HISTORICAL_VERSION_INDEX: Final = 71_332
_FULL_STATUS_MAX_PEAK_BYTES: Final = 48 * 1024 * 1024
_FULL_STATUS_PAYLOAD_BLOB_BYTES: Final = 16

_REPRESENTATIVE_SMALL_TOTAL: Final = 500
_REPRESENTATIVE_LARGE_TOTAL: Final = 2_000
_REPRESENTATIVE_BASE_PAYLOAD_BLOB_BYTES: Final = 512
_REPRESENTATIVE_HEAVY_PAYLOAD_BLOB_BYTES: Final = 8 * 1024
_REPRESENTATIVE_HEAVY_TOTAL: Final = 1_000
_REPRESENTATIVE_PAYLOAD_DELTA_PER_ROW: Final = (
    _REPRESENTATIVE_HEAVY_PAYLOAD_BLOB_BYTES - _REPRESENTATIVE_BASE_PAYLOAD_BLOB_BYTES
)
_REPRESENTATIVE_ALL_PAYLOAD_DELTA_BYTES: Final = (
    _REPRESENTATIVE_HEAVY_TOTAL * _REPRESENTATIVE_PAYLOAD_DELTA_PER_ROW
)
_REPRESENTATIVE_LIVE_PAGE_PAYLOAD_DELTA_BYTES: Final = 100 * _REPRESENTATIVE_PAYLOAD_DELTA_PER_ROW
_REPRESENTATIVE_MAX_PAYLOAD_DELTA_BYTES: Final = 4 * 1024 * 1024

# The 16-byte full-cardinality blob intentionally replaces the former 512-byte blob.
# CI collection required: pin these final-fixture digests from the first optimized Linux
# child result. They must never be computed from the implementation under test here.
_EXPECTED_FULL_STATUS_INVENTORY_DIGEST = (
    "sha256:b66fc2cc8bf283520d4267ae50bdc5016dad4f273520d4c2e123ef13e116c48a"
)
_EXPECTED_FULL_STATUS_SOURCE_RECORDS_DIGEST = (
    "sha256:e0d360c4b625b45028654d295354047f0a4615965f3921a74af893b6a2813c3a"
)
_EXPECTED_FULL_STATUS_SOURCE_INSTANCE_DIGEST = (
    "sha256:67679848766408d54613348f3dd9dd46e6d63a580583c456881ed7c61d18a1ef"
)
_EXPECTED_FULL_STATUS_STALE_RUN_EVIDENCE_DIGEST = (
    "sha256:ab53a30e6005f1d8ceccb465c0dbea1cc743bde9560dfc0b488753ab3f171f8d"
)
_EXPECTED_FULL_STATUS_CONTROL_DIGEST = (
    "sha256:4efb98a775c68e5d1ce88d7454ac4c4051da9d94fd15adbb9072f21c2fccd3f1"
)
_EXPECTED_FULL_STATUS_BOUNDARY_DIGEST = (
    "sha256:193b4d33386c9290cc4eff340a9b5412803eeddcf72150721a0a75d2972e6a9d"
)

_FULL_STATUS_EXPECTED_METRICS: Final[dict[str, ProbeMetric]] = {
    "inventory_row_count": _FULL_STATUS_TOTAL,
    "eligible_unit_count": 178_322,
    "negative_control_count": 6,
    "control_index_0": 0,
    "control_index_1": 35_665,
    "control_index_2": 71_331,
    "control_index_3": 106_997,
    "control_index_4": 142_663,
    "control_index_5": 178_327,
    "historical_version_index": _FULL_STATUS_HISTORICAL_VERSION_INDEX,
    "historical_version_fixture_seen": True,
    "unicode_payload_fixture_seen": True,
    "inventory_digest": _EXPECTED_FULL_STATUS_INVENTORY_DIGEST,
    "source_records_digest": _EXPECTED_FULL_STATUS_SOURCE_RECORDS_DIGEST,
    "source_instance_digest": _EXPECTED_FULL_STATUS_SOURCE_INSTANCE_DIGEST,
    "stale_run_evidence_digest": _EXPECTED_FULL_STATUS_STALE_RUN_EVIDENCE_DIGEST,
    "control_digest": _EXPECTED_FULL_STATUS_CONTROL_DIGEST,
    "boundary_digest": _EXPECTED_FULL_STATUS_BOUNDARY_DIGEST,
    "live_result_cleared": True,
    "all_results_consumed": True,
    "data_call_count": 0,
    "max_live_results": 1,
    "max_active_page_size": 100,
    "max_projection_param_size": 100,
    "max_source_param_size": 100,
    "max_rows_per_result": 100,
}


@dataclass(frozen=True)
class _TraceMeasurement:
    boundary: RepairBoundarySnapshot
    transaction: _GuardedStatusTransaction
    peak_bytes: int


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
        self._transaction.consumed_result_count += 1
        self._transaction.live_result = None

    def data(self) -> list[dict[str, JsonValue]]:
        self._transaction.data_call_count += 1
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
    def __init__(
        self,
        total: int,
        negative_control_indices: frozenset[int],
        historical_version_index: int,
        *,
        payload_blob_bytes: int = _FULL_STATUS_PAYLOAD_BLOB_BYTES,
        distinct_payloads: bool = False,
    ) -> None:
        self.total = total
        self.negative_control_indices = negative_control_indices
        self.historical_version_index = historical_version_index
        self.live_result: _GuardedResult | None = None
        self.max_live_results = 0
        self.max_active_page_size = 0
        self.max_projection_param_size = 0
        self.max_source_param_size = 0
        self.max_rows_per_result = 0
        self.consumed_result_count = 0
        self.created_result_count = 0
        self.data_call_count = 0
        self.historical_version_fixture_seen = False
        self.unicode_payload_fixture_seen = False
        self._distinct_payloads = distinct_payloads
        self._payload_blob = "x" * payload_blob_bytes
        self._shared_payloads: dict[str, str] = {
            "Ångström": self._payload_value("Ångström"),
            "historical": self._payload_value("historical"),
        }

    def run(self, query: str, **parameters: object) -> _GuardedResult:
        if self.live_result is not None:
            raise AssertionError("status opened a nested query before consuming the prior result")
        rows = self._rows(query, parameters)
        result = _GuardedResult(self, rows)
        self.live_result = result
        self.created_result_count += 1
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
        current_version = "2" if index == self.historical_version_index else "1"
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
                "raw_payload": self._payload("Ångström", index),
                "normalized_payload": "{}",
            }
        ]
        if index == self.historical_version_index:
            self.historical_version_fixture_seen = True
            logical_versions.insert(
                0,
                {
                    "source_record_pk": f"history-{index}",
                    "source_record_version": "1",
                    "lifecycle_status": "superseded",
                    "is_latest": False,
                    "raw_payload": self._payload("historical", index),
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
            "raw_payload": self._payload("Ångström", index),
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

    def _payload(self, marker: str, index: int) -> str:
        if marker != "historical":
            self.unicode_payload_fixture_seen = True
        if self._distinct_payloads:
            return self._payload_value(marker, index)
        return self._shared_payloads[marker]

    def _payload_value(self, marker: str, index: int | None = None) -> str:
        row_prefix = "" if index is None else f"{index:06d}:"
        return (
            '{"crm_deal_identity_policy_version":"legacy","marker":"'
            + marker
            + '","blob":"'
            + row_prefix
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


@pytest.mark.large_boundary
def test_status_snapshot_streams_full_high_cardinality_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_child_probe(Path(__file__), _FULL_STATUS_WORKLOAD)
    with capsys.disabled():
        print(format_probe_evidence(result))
    assert result.elapsed_seconds > 0
    assert result.peak_rss_bytes < 2 * 1024 * 1024 * 1024
    assert result.metrics == _FULL_STATUS_EXPECTED_METRICS, format_probe_metrics(result)


def test_status_snapshot_traced_allocations_are_bounded_and_payload_insensitive() -> None:
    small = _traced_status_snapshot(
        _REPRESENTATIVE_SMALL_TOTAL,
        payload_blob_bytes=_REPRESENTATIVE_BASE_PAYLOAD_BLOB_BYTES,
        distinct_payloads=True,
    )
    large = _traced_status_snapshot(
        _REPRESENTATIVE_LARGE_TOTAL,
        payload_blob_bytes=_REPRESENTATIVE_BASE_PAYLOAD_BLOB_BYTES,
        distinct_payloads=True,
    )
    payload_heavy = _traced_status_snapshot(
        _REPRESENTATIVE_HEAVY_TOTAL,
        payload_blob_bytes=_REPRESENTATIVE_HEAVY_PAYLOAD_BLOB_BYTES,
        distinct_payloads=True,
    )

    for measurement in (small, large, payload_heavy):
        _assert_representative_snapshot(measurement)
        assert measurement.peak_bytes < _FULL_STATUS_MAX_PEAK_BYTES

    # The frozen PK tuple is permitted O(N) state. Retaining payload-bearing pages/results is not.
    assert large.peak_bytes - small.peak_bytes < 5 * 1024 * 1024
    # A 100-row live page adds only 768,000 raw bytes. The 4 MiB allowance permits
    # canonicalization slack, but stays well below the 7,680,000 bytes required to retain
    # the additional distinct payload bytes for all 1,000 heavy rows.
    assert (
        _REPRESENTATIVE_MAX_PAYLOAD_DELTA_BYTES
        < _REPRESENTATIVE_ALL_PAYLOAD_DELTA_BYTES
    )
    assert (
        _REPRESENTATIVE_LIVE_PAGE_PAYLOAD_DELTA_BYTES
        < _REPRESENTATIVE_MAX_PAYLOAD_DELTA_BYTES
    )
    assert payload_heavy.peak_bytes - small.peak_bytes < _REPRESENTATIVE_MAX_PAYLOAD_DELTA_BYTES


def _traced_status_snapshot(
    total: int,
    *,
    payload_blob_bytes: int,
    distinct_payloads: bool,
) -> _TraceMeasurement:
    expected_pks, transaction = _status_fixture(
        total,
        payload_blob_bytes=payload_blob_bytes,
        distinct_payloads=distinct_payloads,
    )
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
    return _TraceMeasurement(boundary, transaction, peak_bytes)


def _assert_representative_snapshot(measurement: _TraceMeasurement) -> None:
    transaction = measurement.transaction
    boundary = measurement.boundary
    assert boundary.inventory_row_count == transaction.total
    assert boundary.negative_control_count == 6
    assert boundary.eligible_unit_count == transaction.total - 6
    assert transaction.historical_version_fixture_seen
    assert transaction.unicode_payload_fixture_seen
    assert transaction.live_result is None
    assert transaction.created_result_count == transaction.consumed_result_count
    assert transaction.data_call_count == 0
    assert transaction.max_live_results == 1
    assert transaction.max_active_page_size == 100
    assert transaction.max_projection_param_size == 100
    assert transaction.max_source_param_size == 100
    assert transaction.max_rows_per_result == 100


def _status_fixture(
    total: int,
    *,
    payload_blob_bytes: int = _FULL_STATUS_PAYLOAD_BLOB_BYTES,
    distinct_payloads: bool = False,
) -> tuple[tuple[str, ...], _GuardedStatusTransaction]:
    negative_control_indices = _negative_control_indices(total)
    historical_version_index = _historical_version_index(total, negative_control_indices)
    expected_pks = tuple(f"pk-{index:06d}" for index in range(total))
    transaction = _GuardedStatusTransaction(
        total,
        negative_control_indices,
        historical_version_index,
        payload_blob_bytes=payload_blob_bytes,
        distinct_payloads=distinct_payloads,
    )
    return expected_pks, transaction


def _negative_control_indices(total: int) -> frozenset[int]:
    if total == _FULL_STATUS_TOTAL:
        return _FULL_STATUS_NEGATIVE_CONTROL_INDICES
    return frozenset({0, total // 5, 2 * total // 5, total // 2, 4 * total // 5, total - 1})


def _historical_version_index(total: int, controls: frozenset[int]) -> int:
    if total == _FULL_STATUS_TOTAL:
        return _FULL_STATUS_HISTORICAL_VERSION_INDEX
    historical_index = total // 2 + 1
    if historical_index in controls:
        raise AssertionError("representative historical fixture overlaps a negative control")
    return historical_index


def _emit_full_status_probe() -> None:
    expected_pks, transaction = _status_fixture(_FULL_STATUS_TOTAL)
    started = time.perf_counter()
    boundary = status_snapshot.status_snapshot_from_transaction(
        transaction,
        "portal-a",
        "portal-a",
        expected_pks,
    )
    elapsed_seconds = time.perf_counter() - started
    emit_child_probe_result(
        _FULL_STATUS_WORKLOAD,
        elapsed_seconds,
        _full_status_probe_metrics(boundary, transaction),
    )


def _full_status_probe_metrics(
    boundary: RepairBoundarySnapshot,
    transaction: _GuardedStatusTransaction,
) -> dict[str, ProbeMetric]:
    controls = tuple(sorted(transaction.negative_control_indices))
    if len(controls) != 6:
        raise AssertionError("full status fixture must contain six negative controls")
    return {
        "inventory_row_count": boundary.inventory_row_count,
        "eligible_unit_count": boundary.eligible_unit_count,
        "negative_control_count": boundary.negative_control_count,
        "control_index_0": controls[0],
        "control_index_1": controls[1],
        "control_index_2": controls[2],
        "control_index_3": controls[3],
        "control_index_4": controls[4],
        "control_index_5": controls[5],
        "historical_version_index": transaction.historical_version_index,
        "historical_version_fixture_seen": transaction.historical_version_fixture_seen,
        "unicode_payload_fixture_seen": transaction.unicode_payload_fixture_seen,
        "inventory_digest": boundary.inventory_digest,
        "source_records_digest": boundary.source_records_digest,
        "source_instance_digest": boundary.source_instance_digest,
        "stale_run_evidence_digest": boundary.stale_run_evidence_digest,
        "control_digest": boundary.control_digest,
        "boundary_digest": boundary.boundary_digest,
        "live_result_cleared": transaction.live_result is None,
        "all_results_consumed": (
            transaction.created_result_count == transaction.consumed_result_count
        ),
        "data_call_count": transaction.data_call_count,
        "max_live_results": transaction.max_live_results,
        "max_active_page_size": transaction.max_active_page_size,
        "max_projection_param_size": transaction.max_projection_param_size,
        "max_source_param_size": transaction.max_source_param_size,
        "max_rows_per_result": transaction.max_rows_per_result,
    }


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


if child_probe_requested(sys.argv, _FULL_STATUS_WORKLOAD):
    _emit_full_status_probe()
