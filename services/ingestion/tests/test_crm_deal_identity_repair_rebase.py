"""Focused contract coverage for issue #424's non-executable boundary rebase."""

from __future__ import annotations

import gc
import json
import tracemalloc
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
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
)
from src.crm_deal_identity_repair.digests import object_digest
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


def _request() -> RepairBoundaryRebaseRequest:
    return RepairBoundaryRebaseRequest(
        RepairControlRequest("repair-424", "run-424", "owner-424", "secret-424", 5),
        "approval-424",
        "fresh-artifact-424",
        _DIGEST,
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
    assert "status_snapshot_from_transaction" in source
    assert "_snapshot_from_transaction" not in source
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

    def __init__(self, total: int) -> None:
        self.total = total
        self.calls = 0
        self.active_iterators = 0
        self.maximum_active_iterators = 0
        self.payload = "x" * 512

    def lines(self) -> Iterator[bytes]:
        self.calls += 1
        self.active_iterators += 1
        self.maximum_active_iterators = max(self.maximum_active_iterators, self.active_iterators)
        try:
            for index in range(self.total):
                partition = (
                    "negative_control" if index in _NEGATIVE_CONTROL_INDICES else "ownership_repair"
                )
                yield (
                    json.dumps(
                        {
                            "source_system": "bitrix_chat",
                            "source_record_id": f"bitrix-crm-deal-{index:06d}",
                            "source_record_pk": f"pk-{index:06d}",
                            "deal_id": f"{index:06d}",
                            "partition": partition,
                            "repair_conditions": [partition],
                            "graph_fingerprint": _DIGEST,
                            "stored_payload_fingerprint": _OTHER_DIGEST,
                            "payload": {"payload_blob": self.payload},
                            "execution_allowed": False,
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                    + b"\n"
                )
        finally:
            self.active_iterators -= 1


def _high_cardinality_overlay(total: int) -> ApprovalOverlay:
    rows = tuple(
        ApprovalRow(
            f"bitrix_chat|bitrix-crm-deal-{index:06d}|pk-{index:06d}",
            f"pk-{index:06d}",
            _DIGEST,
            _OTHER_DIGEST,
            "blocked" if index in _NEGATIVE_CONTROL_INDICES else "executable",
        )
        for index in range(total)
    )
    negative_count = sum(index in _NEGATIVE_CONTROL_INDICES for index in range(total))
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


def test_rebase_compact_preparation_streams_178328_rows_without_retaining_payloads_or_units() -> (
    None
):
    """The preparation contract retains approval rows, never artifact payloads or all units."""
    total = 178_328
    overlay = _high_cardinality_overlay(total)
    source = _InventoryLineSource(total)

    tracemalloc.start()
    try:
        evidence = stream_rebase_allocation_evidence(
            run_id="run-424",
            boundary_digest=_DIGEST,
            overlay=overlay,
            inventory_lines=source.lines,
            batch_size=250,
        )
        maximum_batch_size = 0
        observed_unit_count = 0
        for batch in evidence.unit_batches():
            maximum_batch_size = max(maximum_batch_size, len(batch))
            observed_unit_count += len(batch)
            assert all("payload" not in unit for unit in batch)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    gc.collect()
    assert evidence.completion.unit_count == total - len(_NEGATIVE_CONTROL_INDICES)
    assert observed_unit_count == evidence.completion.unit_count
    assert maximum_batch_size == 250
    assert source.calls == 3
    assert source.active_iterators == 0
    assert source.maximum_active_iterators == 1
    assert set(vars(evidence)) == {
        "completion",
        "unit_set_digest",
        "unit_ids",
        "_unit_batches",
    }
    assert evidence.unit_ids[0]
    assert "units" not in vars(evidence)
    assert peak_bytes < 64 * 1024 * 1024


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
