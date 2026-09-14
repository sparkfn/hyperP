"""Focused contract coverage for issue #424's non-executable boundary rebase."""

from __future__ import annotations

import ast
import gc
import json
import sys
import time
import tracemalloc
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from _crm_deal_identity_repair_probe import (
    ProbeMetric,
    child_probe_requested,
    emit_child_probe_result,
    format_probe_evidence,
    format_probe_metrics,
    run_child_probe,
)
from src.crm_deal_identity_repair.allocation import (
    RebaseAllocationEvidence,
    plan_allocation,
    stream_rebase_allocation_evidence,
)
from src.crm_deal_identity_repair.approval_overlay import ApprovalOverlay, ApprovalRow
from src.crm_deal_identity_repair.cli import parse_arguments
from src.crm_deal_identity_repair.control_models import (
    RepairAllocationCompletion,
    RepairControlRequest,
    RepairDispatchLease,
    _trusted_request_from_durable_digest,
)
from src.crm_deal_identity_repair.digests import (
    CanonicalObjectDigest,
    canonical_json_line,
    object_digest,
)
from src.crm_deal_identity_repair.models import inventory_item_from_json
from src.crm_deal_identity_repair.rebase import (
    RepairBoundaryRebaseRequest,
    RepairBoundaryRebaseResult,
    rebase_audit_digest,
    rebase_hmac,
    rebase_receipt_digest,
    validate_rebase_hmac,
)
from src.graph.queries import crm_deal_identity_repair_rebase as queries

_DIGEST = "sha256:" + "a" * 64
_OTHER_DIGEST = "sha256:" + "b" * 64
_NEGATIVE_CONTROL_INDICES = frozenset({0, 35_665, 71_331, 106_997, 142_663, 178_327})
_FULL_REBASE_PROBE_WORKLOAD = "rebase-178328"
_FULL_REBASE_TOTAL = 178_328
_FULL_REBASE_ELIGIBLE = 178_322
_FULL_REBASE_BATCH_SIZE = 250
_FULL_REBASE_PAYLOAD_BYTES = 0
_MAX_REBASE_TRACED_BYTES = 64 * 1024 * 1024
_REPRESENTATIVE_SMALL_TOTAL = 500
_REPRESENTATIVE_LARGE_TOTAL = 2_000
_REPRESENTATIVE_BASELINE_PAYLOAD_BYTES = 128
_REPRESENTATIVE_PAYLOAD_BYTES = 8 * 1024
_MAX_REPRESENTATIVE_GROWTH_BYTES = 2 * 1024 * 1024
_MAX_PAYLOAD_SENSITIVITY_BYTES = 2 * 1024 * 1024

# Payload bytes are deliberately excluded from compact allocation identity: the
# production rebase parser constructs payload={} and _unit binds only inventory
# identity plus the graph/stored fingerprints. These #1056 values therefore remain
# valid after shrinking this full-cardinality fixture's payload.
_EXPECTED_FULL_REBASE_COMPLETION_ID = "925309a9-5d3e-5987-842d-e455ca9ae580"
_EXPECTED_FULL_REBASE_ALLOCATION_DIGEST = (
    "sha256:6c2b1c1b51bc7da175eb43d75972180bfa22d91b506129726994f5faec1b5cc2"
)
_EXPECTED_FULL_REBASE_UNIT_SET_DIGEST = (
    "sha256:b0334124f8fbc93d1cf463fe251375aa763ca105129f54f8c859f66dd68d7a6a"
)
_EXPECTED_FULL_REBASE_FIRST_UNIT_ID = "d36ae152-0ad4-5d98-9925-518191e2cd7b"
_EXPECTED_FULL_REBASE_MIDDLE_UNIT_ID = "54ab6917-8b63-59c5-8341-5f226a21828b"
_EXPECTED_FULL_REBASE_LAST_UNIT_ID = "978185a5-d370-5330-9740-514b3cd5a443"


def _request() -> RepairBoundaryRebaseRequest:
    return RepairBoundaryRebaseRequest(
        RepairControlRequest("repair-424", "run-424", "owner-424", "secret-424", 5),
        "approval-424",
        "fresh-artifact-424",
        _DIGEST,
    )


def test_rebase_request_accepts_trusted_durable_control_without_rehashing() -> None:
    raw_control = RepairControlRequest(
        "repair-424",
        "run-424",
        "owner-424",
        "secret-424",
        5,
    )
    durable_control = _trusted_request_from_durable_digest(
        raw_control.repair_id,
        raw_control.run_id,
        raw_control.owner_id,
        raw_control.token_digest,
        raw_control.expected_revision,
    )
    raw_request = RepairBoundaryRebaseRequest(
        raw_control,
        "approval-424",
        "fresh-artifact-424",
        _DIGEST,
    )
    durable_request = RepairBoundaryRebaseRequest(
        durable_control,
        "approval-424",
        "fresh-artifact-424",
        _DIGEST,
    )

    assert durable_request.to_dict() == raw_request.to_dict()
    assert durable_request.request_digest == raw_request.request_digest


def test_status_evidence_reexports_canonical_digest_helpers() -> None:
    from src.graph import crm_deal_identity_repair_status_evidence as status_evidence

    assert status_evidence.CanonicalObjectDigest is CanonicalObjectDigest
    assert status_evidence.canonical_json_line is canonical_json_line


def test_canonical_incremental_array_digest_matches_legacy_object_digest() -> None:
    domain = b"crm-deal-identity-repair-status-parity-v1\x00"
    values = [
        {"kind": "first", "sequence": 1},
        {"kind": "second", "sequence": 2},
    ]
    digest = CanonicalObjectDigest(domain)
    digest.value("control", {"state": "allocated"})
    digest.array("rows", (canonical_json_line(value) for value in values))

    assert digest.finish() == object_digest(
        domain,
        {"control": {"state": "allocated"}, "rows": values},
    )


def test_rebase_cli_requires_distinct_fresh_artifact_and_observed_boundary() -> None:
    common = (
        "rebase-boundary",
        "--repair-id",
        "repair-424",
        "--run-id",
        "run-424",
        "--owner-id",
        "owner-424",
        "--expected-revision",
        "5",
        "--approval-id",
        "approval-424",
    )
    with pytest.raises(SystemExit):
        parse_arguments(common)
    with pytest.raises(SystemExit):
        parse_arguments((*common, "--fresh-artifact-id", "fresh-424"))
    parsed = parse_arguments(
        (
            *common,
            "--fresh-artifact-id",
            "fresh-424",
            "--expected-observed-boundary-digest",
            _DIGEST,
        )
    )
    assert parsed.command == "rebase-boundary"
    assert parsed.fresh_artifact_id == "fresh-424"


def test_rebase_audit_and_hmac_bind_old_and_new_authority() -> None:
    request = _request()
    audit = rebase_audit_digest(
        request=request,
        completion_id="completion-424",
        allocation_digest=_DIGEST,
        unit_set_digest=_DIGEST,
        fresh_artifact_manifest_hmac="b" * 64,
        fresh_inventory_digest=_DIGEST,
        fresh_producer_repository_sha="a" * 40,
        fresh_producer_image_digest="sha256:" + "b" * 64,
        previous_boundary_digest=_DIGEST,
        replacement_boundary_digest="sha256:" + "c" * 64,
        replacement_components={
            "source_records_digest": _DIGEST,
            "source_instance_digest": _DIGEST,
            "stale_run_evidence_digest": _DIGEST,
            "control_digest": _DIGEST,
            "inventory_digest": _DIGEST,
            "inventory_row_count": 1,
            "eligible_unit_count": 1,
            "negative_control_count": 0,
        },
        previous_receipt_digest=_DIGEST,
        previous_origin_hmac="d" * 64,
        revision=6,
    )
    signature = rebase_hmac(secret=b"approval-secret", key_id="approval-key", audit_digest=audit)
    validate_rebase_hmac(
        secret=b"approval-secret",
        key_id="approval-key",
        audit_digest=audit,
        supplied_hmac=signature,
    )
    with pytest.raises(RuntimeError, match="HMAC"):
        validate_rebase_hmac(
            secret=b"approval-secret",
            key_id="approval-key",
            audit_digest="sha256:" + "e" * 64,
            supplied_hmac=signature,
        )
    assert rebase_receipt_digest(
        request_digest=request.request_digest,
        run_id="run-424",
        completion_id="completion-424",
        previous_boundary_digest=_DIGEST,
        replacement_boundary_digest="sha256:" + "c" * 64,
        revision=6,
    ) != rebase_receipt_digest(
        request_digest=request.request_digest,
        run_id="run-424",
        completion_id="completion-424",
        previous_boundary_digest=_DIGEST,
        replacement_boundary_digest="sha256:" + "f" * 64,
        revision=6,
    )


def test_rebase_result_remains_nonsecret_and_replay_explicit() -> None:
    request = _request()
    result = RepairBoundaryRebaseResult(
        RepairDispatchLease(
            "control-424",
            "run-424",
            "owner-424",
            request.control.token_digest,
            6,
            "allocated",
            _DIGEST,
        ),
        _DIGEST,
        "sha256:" + "c" * 64,
        _DIGEST,
        "sha256:" + "d" * 64,
        True,
    )
    assert result.replayed is True
    assert result.lease.state == "allocated"


def test_rebase_query_is_guarded_and_never_creates_units_or_dispatches() -> None:
    full_query = "\n".join(
        (
            queries.READ_REBASE_REPLAY,
            queries.LOCK_REBASE_AUTHORITY,
            queries.READ_REBASE_GUARDS,
            queries.ADVANCE_REBASE_CONTROL,
            queries.COMMIT_REBASE_BOUNDARY,
        )
    )
    assert "CREATE (allocated:CrmDealRepairUnit" not in full_query
    assert "SET dispatch.blocked = false" not in full_query
    assert "CrmDealRepairMutationResult" in queries.READ_REBASE_GUARDS
    assert "CrmDealRepairVerification" in queries.READ_REBASE_GUARDS
    assert "CrmDealRepairSecondaryDisposition" in queries.READ_REBASE_GUARDS
    assert "CrmDealRepairFence {run_id: $run_id, state: 'claimed'}" in queries.READ_REBASE_GUARDS
    assert "stored_unit_count = completion.unit_count" in queries.READ_REBASE_GUARDS
    assert "completion_count = 1" in queries.READ_REBASE_REPLAY
    assert "completion_count = 1" in queries.READ_REBASE_REPLAY_INTEGRITY
    assert "stored_count = completion.unit_count" in queries.READ_REBASE_REPLAY_INTEGRITY
    assert "CrmDealRepairMutationResult" in queries.READ_REBASE_REPLAY_INTEGRITY
    assert "CrmDealRepairVerification" in queries.READ_REBASE_REPLAY_INTEGRITY
    assert "CrmDealRepairSecondaryDisposition" in queries.READ_REBASE_REPLAY_INTEGRITY
    assert "inventory_binding_digest = expected.inventory_binding_digest" in (
        queries.READ_REBASE_UNIT_BATCH
    )
    assert "completion.rebase_request_digest IS NULL" in queries.COMMIT_REBASE_BOUNDARY
    assert "SET dispatch.repair_run_id = dispatch.repair_run_id" in queries.LOCK_REBASE_AUTHORITY


def test_rebase_repository_uses_bounded_snapshot_and_transaction_local_effective_reader() -> None:
    from src.graph import crm_deal_identity_repair_rebase as repository

    source = Path(repository.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_public_helper = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "src.graph.crm_deal_identity_repair_status_snapshot"
        and any(alias.name == "status_snapshot_from_transaction" for alias in node.names)
        for node in ast.walk(tree)
    )
    public_call = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "status_snapshot_from_transaction"
        for node in ast.walk(tree)
    )
    private_import_or_call = any(
        (
            isinstance(node, ast.ImportFrom)
            and any(alias.name == "_snapshot_from_transaction" for alias in node.names)
        )
        or (
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "_snapshot_from_transaction"
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "_snapshot_from_transaction"
            )
        )
        for node in ast.walk(tree)
    )
    assert imported_public_helper
    assert public_call
    assert not private_import_or_call
    assert "def effective_boundary_digest_from_transaction" in source
    assert "return self._client.execute_read" in source


def test_rebase_approval_path_rejects_aliases_and_root_escapes(tmp_path: Path) -> None:
    """Rebase must use the same non-aliasing approval-file boundary as integration."""
    import src.crm_deal_identity_repair_control as control_module
    from src.crm_deal_identity_repair.integration_runtime import _approval_overlay_path

    control_source = Path(control_module.__file__).read_text(encoding="utf-8")
    assert "_approval_overlay_path(" in control_source
    assert "overlay.approval_id != arguments.approval_id" in control_source
    assert _approval_overlay_path(str(tmp_path), "approval-424") == tmp_path / "approval-424.json"
    for approval_id in (
        "../approval-424",
        "approval-424/../outside",
        "approval-424\\outside",
        "approval-424.",
        "approval-424 ",
        "CON",
        "con.json",
        "LPT1",
    ):
        with pytest.raises(RuntimeError, match="file identity|escapes"):
            _approval_overlay_path(str(tmp_path), approval_id)


class _InventoryLineSource:
    """Fresh, payload-bearing JSONL passes with observable iterator cleanup."""

    _PREFIX = b'{"source_system":"bitrix_chat","source_record_id":"bitrix-crm-deal-'
    _AFTER_RECORD_ID = b'","source_record_pk":"pk-'
    _AFTER_SOURCE_RECORD_PK = b'","deal_id":"'
    _AFTER_DEAL_ID = b'","partition":"'
    _AFTER_PARTITION = b'","repair_conditions":["'
    _AFTER_CONDITION = (
        b'"],"graph_fingerprint":"'
        + _DIGEST.encode("ascii")
        + b'","stored_payload_fingerprint":"'
        + _OTHER_DIGEST.encode("ascii")
        + b'","payload":'
    )
    _SUFFIX = b',"execution_allowed":false}\n'

    def __init__(
        self,
        total: int,
        negative_control_indices: frozenset[int] = _NEGATIVE_CONTROL_INDICES,
        payload_bytes: int = _REPRESENTATIVE_BASELINE_PAYLOAD_BYTES,
    ) -> None:
        self.total = total
        self.negative_control_indices = negative_control_indices
        self.calls = 0
        self.active_iterators = 0
        self.maximum_active_iterators = 0
        self._payload_json = json.dumps(
            {"payload_blob": "x" * payload_bytes},
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")

    def lines(self) -> Iterator[bytes]:
        self.calls += 1
        self.active_iterators += 1
        self.maximum_active_iterators = max(self.maximum_active_iterators, self.active_iterators)
        try:
            for index in range(self.total):
                partition = (
                    b"negative_control"
                    if index in self.negative_control_indices
                    else b"ownership_repair"
                )
                index_bytes = f"{index:06d}".encode("ascii")
                # All dynamic fragments are either ASCII decimal digits generated
                # locally or one of these fixed JSON strings; payload JSON is
                # encoded once per source, not once per row.
                yield b"".join(
                    (
                        self._PREFIX,
                        index_bytes,
                        self._AFTER_RECORD_ID,
                        index_bytes,
                        self._AFTER_SOURCE_RECORD_PK,
                        index_bytes,
                        self._AFTER_DEAL_ID,
                        partition,
                        self._AFTER_PARTITION,
                        partition,
                        self._AFTER_CONDITION,
                        self._payload_json,
                        self._SUFFIX,
                    )
                )
        finally:
            self.active_iterators -= 1


def _high_cardinality_overlay(
    total: int,
    negative_control_indices: frozenset[int] = _NEGATIVE_CONTROL_INDICES,
) -> ApprovalOverlay:
    rows = tuple(
        ApprovalRow(
            f"bitrix_chat|bitrix-crm-deal-{index:06d}|pk-{index:06d}",
            f"pk-{index:06d}",
            _DIGEST,
            _OTHER_DIGEST,
            "blocked" if index in negative_control_indices else "executable",
        )
        for index in range(total)
    )
    negative_count = sum(index in negative_control_indices for index in range(total))
    return ApprovalOverlay(
        "approval-424",
        "repair-424",
        "run-424",
        _DIGEST,
        "a" * 32,
        "b" * 64,
        _DIGEST,
        total,
        _DIGEST,
        "c" * 40,
        _OTHER_DIGEST,
        _DIGEST,
        "12345678-1234-5678-9234-567812345678",
        "approval-reference",
        total - negative_count,
        rows,
        "approval-key",
        _DIGEST,
    )


def test_rebase_streaming_evidence_matches_legacy_allocation_identity() -> None:
    source = _InventoryLineSource(8)
    raw_rows = [json.loads(line) for line in source.lines()]
    legacy_items = []
    for raw in raw_rows:
        raw["payload"] = {}
        legacy_items.append(inventory_item_from_json(raw))
    overlay = _high_cardinality_overlay(8)
    legacy = plan_allocation(
        run_id="run-424",
        boundary_digest=_DIGEST,
        inventory=tuple(legacy_items),
        overlay=overlay,
    )
    evidence = stream_rebase_allocation_evidence(
        run_id="run-424",
        boundary_digest=_DIGEST,
        overlay=overlay,
        inventory_lines=_InventoryLineSource(8).lines,
        batch_size=3,
    )
    legacy_unit_set = object_digest(
        b"crm-deal-identity-repair-allocation-unit-set-v1\x00",
        {"units": [asdict(unit) for unit in legacy.units]},
    )
    streamed_units = [unit for batch in evidence.unit_batches() for unit in batch]
    assert evidence.completion == legacy.completion
    assert evidence.unit_ids == tuple(unit.unit_id for unit in legacy.units)
    assert evidence.unit_set_digest == legacy_unit_set
    assert streamed_units == [asdict(unit) for unit in legacy.units]


@dataclass(frozen=True)
class _RebaseTraceObservation:
    peak_bytes: int
    observed_unit_count: int
    maximum_batch_size: int
    payload_bearing_unit_count: int


def _consume_unit_batches(evidence: RebaseAllocationEvidence) -> tuple[int, int, int]:
    maximum_batch_size = 0
    observed_unit_count = 0
    payload_bearing_unit_count = 0
    for batch in evidence.unit_batches():
        maximum_batch_size = max(maximum_batch_size, len(batch))
        observed_unit_count += len(batch)
        payload_bearing_unit_count += sum("payload" in unit for unit in batch)
    return maximum_batch_size, observed_unit_count, payload_bearing_unit_count


def _full_rebase_probe_metrics() -> dict[str, ProbeMetric]:
    overlay = _high_cardinality_overlay(_FULL_REBASE_TOTAL)
    source = _InventoryLineSource(
        _FULL_REBASE_TOTAL,
        payload_bytes=_FULL_REBASE_PAYLOAD_BYTES,
    )
    evidence = stream_rebase_allocation_evidence(
        run_id="run-424",
        boundary_digest=_DIGEST,
        overlay=overlay,
        inventory_lines=source.lines,
        batch_size=_FULL_REBASE_BATCH_SIZE,
    )
    maximum_batch_size, observed_unit_count, payload_bearing_unit_count = _consume_unit_batches(
        evidence
    )
    middle_index = len(evidence.unit_ids) // 2
    return {
        "total": _FULL_REBASE_TOTAL,
        "eligible_unit_count": evidence.completion.unit_count,
        "negative_control_count": _FULL_REBASE_TOTAL - evidence.completion.unit_count,
        "completion_run_id": evidence.completion.run_id,
        "completion_id": evidence.completion.completion_id,
        "completion_boundary_digest": evidence.completion.boundary_digest,
        "completion_overlay_digest": evidence.completion.overlay_digest,
        # allocation_digest commits to the complete ordered unit_ids array; unit_set_digest
        # independently commits to the complete canonical executable-unit documents.
        "allocation_digest": evidence.completion.allocation_digest,
        "unit_set_digest": evidence.unit_set_digest,
        "first_unit_id": evidence.unit_ids[0],
        "middle_unit_id": evidence.unit_ids[middle_index],
        "last_unit_id": evidence.unit_ids[-1],
        "unit_ids_count": len(evidence.unit_ids),
        "observed_unit_count": observed_unit_count,
        "maximum_batch_size": maximum_batch_size,
        "payload_bearing_unit_count": payload_bearing_unit_count,
        "source_calls": source.calls,
        "active_iterators": source.active_iterators,
        "maximum_active_iterators": source.maximum_active_iterators,
        "evidence_fields": ",".join(sorted(vars(evidence))),
        "has_units_attribute": "units" in vars(evidence),
    }


def _run_full_rebase_probe() -> None:
    started = time.perf_counter()
    metrics = _full_rebase_probe_metrics()
    emit_child_probe_result(
        _FULL_REBASE_PROBE_WORKLOAD,
        time.perf_counter() - started,
        metrics,
    )


def _expected_full_rebase_metrics() -> dict[str, ProbeMetric]:
    return {
        "total": _FULL_REBASE_TOTAL,
        "eligible_unit_count": _FULL_REBASE_ELIGIBLE,
        "negative_control_count": len(_NEGATIVE_CONTROL_INDICES),
        "completion_run_id": "run-424",
        "completion_id": _EXPECTED_FULL_REBASE_COMPLETION_ID,
        "completion_boundary_digest": _DIGEST,
        "completion_overlay_digest": _DIGEST,
        "allocation_digest": _EXPECTED_FULL_REBASE_ALLOCATION_DIGEST,
        "unit_set_digest": _EXPECTED_FULL_REBASE_UNIT_SET_DIGEST,
        "first_unit_id": _EXPECTED_FULL_REBASE_FIRST_UNIT_ID,
        "middle_unit_id": _EXPECTED_FULL_REBASE_MIDDLE_UNIT_ID,
        "last_unit_id": _EXPECTED_FULL_REBASE_LAST_UNIT_ID,
        "unit_ids_count": _FULL_REBASE_ELIGIBLE,
        "observed_unit_count": _FULL_REBASE_ELIGIBLE,
        "maximum_batch_size": _FULL_REBASE_BATCH_SIZE,
        "payload_bearing_unit_count": 0,
        "source_calls": 3,
        "active_iterators": 0,
        "maximum_active_iterators": 1,
        "evidence_fields": "_unit_batches,completion,unit_ids,unit_set_digest",
        "has_units_attribute": False,
    }


def _representative_negative_controls(total: int) -> frozenset[int]:
    return frozenset({0, total // 5, 2 * total // 5, 3 * total // 5, 4 * total // 5, total - 1})


def _traced_rebase_observation(total: int, payload_bytes: int) -> _RebaseTraceObservation:
    negative_controls = _representative_negative_controls(total)
    overlay = _high_cardinality_overlay(total, negative_controls)
    source = _InventoryLineSource(total, negative_controls, payload_bytes)

    tracemalloc.start()
    try:
        evidence = stream_rebase_allocation_evidence(
            run_id="run-424",
            boundary_digest=_DIGEST,
            overlay=overlay,
            inventory_lines=source.lines,
            batch_size=_FULL_REBASE_BATCH_SIZE,
        )
        maximum_batch_size, observed_unit_count, payload_bearing_unit_count = (
            _consume_unit_batches(evidence)
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert evidence.completion.unit_count == total - len(negative_controls)
    assert observed_unit_count == evidence.completion.unit_count
    assert maximum_batch_size == _FULL_REBASE_BATCH_SIZE
    assert payload_bearing_unit_count == 0
    assert source.calls == 3
    assert source.active_iterators == 0
    assert source.maximum_active_iterators == 1
    assert set(vars(evidence)) == {
        "completion",
        "unit_set_digest",
        "unit_ids",
        "_unit_batches",
    }
    assert "units" not in vars(evidence)
    gc.collect()
    return _RebaseTraceObservation(
        peak_bytes,
        observed_unit_count,
        maximum_batch_size,
        payload_bearing_unit_count,
    )


def test_rebase_compact_preparation_retains_only_bounded_streaming_state() -> None:
    """Trace representative scales and payloads without tracing production cardinality."""
    small = _traced_rebase_observation(
        _REPRESENTATIVE_SMALL_TOTAL,
        _REPRESENTATIVE_BASELINE_PAYLOAD_BYTES,
    )
    large = _traced_rebase_observation(
        _REPRESENTATIVE_LARGE_TOTAL,
        _REPRESENTATIVE_BASELINE_PAYLOAD_BYTES,
    )
    payload_sensitive = _traced_rebase_observation(
        _REPRESENTATIVE_SMALL_TOTAL,
        _REPRESENTATIVE_PAYLOAD_BYTES,
    )

    for observation in (small, large, payload_sensitive):
        assert observation.peak_bytes < _MAX_REBASE_TRACED_BYTES
        assert observation.payload_bearing_unit_count == 0
    assert large.observed_unit_count == _REPRESENTATIVE_LARGE_TOTAL - len(
        _representative_negative_controls(_REPRESENTATIVE_LARGE_TOTAL)
    )
    assert large.maximum_batch_size == _FULL_REBASE_BATCH_SIZE
    assert large.peak_bytes - small.peak_bytes < _MAX_REPRESENTATIVE_GROWTH_BYTES
    assert payload_sensitive.peak_bytes - small.peak_bytes < _MAX_PAYLOAD_SENSITIVITY_BYTES


@pytest.mark.large_boundary
def test_rebase_compact_preparation_streams_178328_rows_without_retaining_payloads_or_units(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run exact production-cardinality streaming evidence in an isolated process."""
    result = run_child_probe(Path(__file__), _FULL_REBASE_PROBE_WORKLOAD)
    with capsys.disabled():
        print(format_probe_evidence(result))

    assert result.elapsed_seconds > 0
    assert result.peak_rss_bytes < 2 * 1024 * 1024 * 1024
    assert result.metrics == _expected_full_rebase_metrics(), format_probe_metrics(result)


def test_rebase_rejects_counts_that_are_internally_consistent_but_not_recomputable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rebase rejects a signed-looking count projection that disagrees with inventory replay."""
    from contextlib import nullcontext

    import src.crm_deal_identity_repair.artifacts as artifacts_module
    import src.crm_deal_identity_repair.qualification as qualification_module
    import src.crm_deal_identity_repair.qualification_inventory as inventory_module
    import src.crm_deal_identity_repair_control as control_module

    signed_counts = {
        "active_deal_count": 7,
        "authoritative_version_count": 7,
        "active_link_count": 7,
        "active_distinct_owner_count": 7,
        "multi_linked_deal_count": 1,
        "maximum_links_per_deal": 1,
        "maximum_distinct_owners_per_deal": 1,
        "projection_cleanup_deal_count": 0,
        "clean_deal_count": 6,
    }
    recomputed_counts = {**signed_counts, "clean_deal_count": 5}
    run = SimpleNamespace(
        repair_id="repair-424",
        inventory_digest=_DIGEST,
        inventory_row_count=7,
        eligible_unit_count=1,
        negative_control_count=6,
        manifest=SimpleNamespace(source_contract_uuid="12345678-1234-5678-9234-567812345678"),
    )
    original = SimpleNamespace(
        manifest=SimpleNamespace(artifact_id="a" * 32),
        inventory_digest=_DIGEST,
        inventory_row_count=7,
        eligible_unit_count=1,
        negative_control_count=6,
        inventory_source_record_pks=("pk-000001",),
        population_counts=signed_counts,
    )
    fresh = SimpleNamespace(
        manifest=SimpleNamespace(artifact_id="b" * 32),
        inventory_digest=_DIGEST,
        inventory_row_count=7,
        eligible_unit_count=1,
        negative_control_count=6,
        inventory_source_record_pks=("pk-000001",),
        population_counts=signed_counts,
    )
    settings = SimpleNamespace(
        crm_deal_identity_repair_approval_key_secret=SimpleNamespace(
            get_secret_value=lambda: "approval-secret"
        ),
        crm_deal_identity_repair_approval_key_id="approval-key",
    )
    arguments = SimpleNamespace(fresh_artifact_id="b" * 32, approval_id="approval-424")

    monkeypatch.setattr(
        artifacts_module,
        "repair_artifact_store_from_settings",
        lambda _settings: nullcontext(object()),
    )
    monkeypatch.setattr(
        artifacts_module,
        "repair_inventory_configuration_digest",
        lambda _settings: _DIGEST,
    )
    monkeypatch.setattr(
        qualification_module,
        "verify_qualified_repair_artifact",
        lambda _store, *, run: original,
    )
    monkeypatch.setattr(
        qualification_module,
        "verify_rebase_artifact",
        lambda _store, **_kwargs: fresh,
    )
    monkeypatch.setattr(
        qualification_module,
        "iter_verified_inventory_lines",
        lambda _artifact: iter((b"inventory-row\n",)),
    )
    monkeypatch.setattr(
        inventory_module,
        "recompute_population_counts_from_lines",
        lambda _lines: recomputed_counts,
    )

    with pytest.raises(RuntimeError, match="population counts are not recomputable"):
        control_module._rebase_boundary(
            arguments,
            settings,
            SimpleNamespace(),
            run,
            _request().control,
        )


class _ReplayResult:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def single(self) -> object | None:
        return self.value


class _ReplayTransaction:
    def __init__(self, rejection: str) -> None:
        self.rejection = rejection
        self.integrity_parameters: dict[str, object] | None = None
        self.unit_batch_parameters: list[dict[str, object]] = []

    def run(self, query: str, **parameters: object) -> _ReplayResult:
        from src.graph.queries.crm_deal_identity_repair_ledger import GET_REPAIR_RUN

        if query == queries.READ_REBASE_REPLAY:
            return _ReplayResult(
                {
                    "control": {"sealed_boundary_digest": _DIGEST},
                    "completion": {
                        "rebase_fresh_artifact_manifest_hmac": "c" * 64,
                        "rebase_fresh_inventory_digest": _DIGEST,
                        "rebase_fresh_producer_repository_sha": "d" * 40,
                        "rebase_fresh_producer_image_digest": _OTHER_DIGEST,
                    },
                }
            )
        if query == GET_REPAIR_RUN:
            return _ReplayResult({"stored": "qualification"})
        if query == queries.READ_REBASE_REPLAY_INTEGRITY:
            self.integrity_parameters = parameters
            if self.rejection == "execution_evidence":
                return _ReplayResult(None)
            return _ReplayResult({"completion_id": "completion-424"})
        if query == queries.READ_REBASE_UNIT_BATCH:
            self.unit_batch_parameters.append(parameters)
            return _ReplayResult(None)
        raise AssertionError("unexpected replay query")


class _ReplayClient:
    def __init__(self, transaction: _ReplayTransaction) -> None:
        self.transaction = transaction

    def execute_read(self, work: object) -> object:
        assert callable(work)
        return work(self.transaction)


def _replay_evidence() -> RebaseAllocationEvidence:
    completion = RepairAllocationCompletion(
        "run-424", "completion-424", _DIGEST, _DIGEST, _DIGEST, 1
    )
    unit = {
        "run_id": "run-424",
        "unit_id": "unit-424",
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
        "boundary_digest": _DIGEST,
        "inventory_fingerprint": _DIGEST,
        "state": "allocated",
        "inventory_key": "bitrix_chat|bitrix-crm-deal-000001|pk-000001",
        "source_record_pk": "pk-000001",
        "inventory_graph_fingerprint": _DIGEST,
        "inventory_stored_payload_fingerprint": _OTHER_DIGEST,
        "inventory_binding_digest": _DIGEST,
    }
    return RebaseAllocationEvidence(
        completion,
        _DIGEST,
        ("unit-424",),
        lambda: iter(([unit],)),
    )


def _replay_repository(
    monkeypatch: pytest.MonkeyPatch,
    transaction: _ReplayTransaction,
    *,
    observed_boundary: str,
) -> object:
    from src.graph import crm_deal_identity_repair_rebase as repository_module

    monkeypatch.setattr(
        repository_module,
        "stored_qualification_from_record",
        lambda _repair_id, _record: SimpleNamespace(
            run=SimpleNamespace(source_instance_id="portal-a", control_instance_id="portal-a"),
            source_record_pks=("pk-000001",),
        ),
    )
    monkeypatch.setattr(
        repository_module,
        "status_snapshot_from_transaction",
        lambda *_args: SimpleNamespace(boundary_digest=observed_boundary),
    )
    return repository_module.CrmDealRepairRebaseRepository(_ReplayClient(transaction))


@pytest.mark.parametrize("rejection", ("missing_unit", "corrupt_unit", "execution_evidence"))
def test_rebase_replay_rejects_missing_corrupt_or_execution_evidence(
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
) -> None:
    """A replay never trusts a completion when its stored-unit proof rejects it."""
    transaction = _ReplayTransaction(rejection)
    repository = _replay_repository(
        monkeypatch,
        transaction,
        observed_boundary=_DIGEST,
    )

    error = (
        "allocation integrity rejected"
        if rejection == "execution_evidence"
        else "stored unit batch differs"
    )
    with pytest.raises(RuntimeError, match=error):
        repository.rebase_boundary(
            _request(),
            evidence=_replay_evidence(),
            fresh_artifact_manifest_hmac="c" * 64,
            fresh_inventory_digest=_DIGEST,
            fresh_producer_repository_sha="d" * 40,
            fresh_producer_image_digest=_OTHER_DIGEST,
            approval_key_id="approval-key",
            approval_secret=b"approval-secret",
        )

    assert rejection in {"missing_unit", "corrupt_unit", "execution_evidence"}
    assert transaction.integrity_parameters is not None
    assert transaction.integrity_parameters["unit_count"] == 1
    assert transaction.integrity_parameters["unit_set_digest"] == _DIGEST
    if rejection == "execution_evidence":
        assert transaction.unit_batch_parameters == []
    else:
        assert transaction.unit_batch_parameters == [
            {"run_id": "run-424", "units": _replay_evidence().unit_batches().__next__()}
        ]


def test_rebase_replay_rechecks_current_boundary_before_accepting_a_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A formerly successful request is not replayable after the graph boundary drifts."""
    transaction = _ReplayTransaction("missing_unit")
    repository = _replay_repository(
        monkeypatch,
        transaction,
        observed_boundary=_OTHER_DIGEST,
    )

    with pytest.raises(RuntimeError, match="replay boundary drift detected"):
        repository.rebase_boundary(
            _request(),
            evidence=_replay_evidence(),
            fresh_artifact_manifest_hmac="c" * 64,
            fresh_inventory_digest=_DIGEST,
            fresh_producer_repository_sha="d" * 40,
            fresh_producer_image_digest=_OTHER_DIGEST,
            approval_key_id="approval-key",
            approval_secret=b"approval-secret",
        )

    assert transaction.integrity_parameters is None


if child_probe_requested(sys.argv, _FULL_REBASE_PROBE_WORKLOAD):
    _run_full_rebase_probe()
