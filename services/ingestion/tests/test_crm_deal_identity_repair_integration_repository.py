"""Executing transaction-adapter coverage for #313 integration CAS bindings."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import TypeVar, cast

import pytest
from neo4j import ManagedTransaction
from src.config import Settings
from src.crm_deal_identity_repair.control_models import RepairControlRequest
from src.crm_deal_identity_repair.digests import object_digest
from src.crm_deal_identity_repair.execution_models import (
    RepairExecutionBoundaryManifest,
    RepairFence,
    RepairMutationResult,
    RepairRollbackImage,
    RepairUnit,
)
from src.crm_deal_identity_repair.execution_status_models import RepairQualificationRun
from src.crm_deal_identity_repair.integration_models import (
    IntegrationOperation,
    RepairIntegrationRequest,
    rollback_status_receipt_digest,
)
from src.crm_deal_identity_repair.integration_service import (
    RepairIntegrationAuthority,
    RepairIntegrationContext,
)
from src.crm_deal_identity_repair.rollback_models import RepairRollbackAuthorization
from src.graph.client import Neo4jClient
from src.graph.crm_deal_identity_repair_integration import (
    CrmDealRepairIntegrationRepository,
    _validated_receipt_binding,
)
from src.graph.queries import crm_deal_identity_repair_integration as queries

T = TypeVar("T")
_DIGEST = "sha256:" + "a" * 64


class _Result:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row

    def single(self) -> dict[str, object] | None:
        return self._row


class _Transaction:
    def __init__(self, rows: dict[str, list[dict[str, object] | None]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **params: object) -> _Result:
        required = set(re.findall(r"\$(\w+)", query))
        missing = required.difference(params)
        if missing:
            raise AssertionError(f"query parameter(s) omitted: {sorted(missing)}")
        self.calls.append((query, params))
        rows = self._rows.get(query, [None])
        return _Result(rows.pop(0) if rows else None)


class _Client:
    def __init__(self, transaction: _Transaction) -> None:
        self.transaction = transaction

    def execute_read(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self.transaction))

    def execute_write(self, work: Callable[[ManagedTransaction], T]) -> T:
        return work(cast(ManagedTransaction, self.transaction))


def _manifest() -> RepairExecutionBoundaryManifest:
    return RepairExecutionBoundaryManifest(
        repair_id="repair",
        artifact_id="a" * 32,
        artifact_manifest_hmac="b" * 64,
        inventory_digest=_DIGEST,
        repository_sha="c" * 40,
        image_digest=_DIGEST,
        configuration_digest=_DIGEST,
        source_contract_uuid="12345678-1234-5678-9234-567812345678",
        environment="staging",
        approval_reference="approval",
        unit_ceiling=2,
        stop_conditions=("boundary_drift",),
        source_instance_id="source",
        control_instance_id="control",
        rollback_authority_reference="review",
        rollback_authority_policy="reviewed_rollback_v1",
        graph_boundary_digest=_DIGEST,
        inventory_row_count=2,
        eligible_unit_count=2,
        negative_control_count=0,
    )


def _context() -> RepairIntegrationContext:
    manifest = _manifest()
    run = RepairQualificationRun(
        manifest.repair_id,
        "run",
        manifest.qualification_identity,
        manifest,
        manifest.graph_boundary_digest,
        "qualified",
    )
    authority = RepairIntegrationAuthority(
        "completion",
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        "key",
        "b" * 64,
        _DIGEST,
        _DIGEST,
        1,
    )
    return RepairIntegrationContext(run, (), authority)


def _request(
    operation: IntegrationOperation, *, unit_id: str | None = "unit"
) -> RepairIntegrationRequest:
    kwargs: dict[str, str] = {}
    if operation in {"rollback-status", "rollback"}:
        kwargs = {
            "authorization_reference": "review",
            "predecessor_transition_id": "predecessor",
        }
    return RepairIntegrationRequest(
        operation,
        RepairControlRequest("repair", "run", "owner", "token", 1),
        "approval",
        unit_id,
        **kwargs,
    )


def _unit(unit_id: str = "unit", sequence: int = 0) -> RepairUnit:
    return RepairUnit(
        "run",
        unit_id,
        1,
        sequence,
        1,
        _DIGEST,
        _DIGEST,
        "applied",
        f"inventory-{unit_id}",
        f"source-{unit_id}",
        _DIGEST,
        _DIGEST,
        _DIGEST,
    )


def _fence(unit: RepairUnit) -> RepairFence:
    return RepairFence(
        unit.run_id,
        unit.unit_id,
        f"fence-{unit.unit_id}",
        unit.generation,
        unit.sequence,
        unit.attempt,
        "owner",
        "sha256:" + "c" * 64,
        unit.boundary_digest,
        _DIGEST,
        "claimed",
    )


def _mutation(unit: RepairUnit, fence: RepairFence) -> RepairMutationResult:
    return RepairMutationResult(
        unit.run_id,
        unit.unit_id,
        f"mutation-{unit.unit_id}",
        unit.generation,
        unit.sequence,
        unit.attempt,
        fence.owner_id,
        fence.token,
        unit.boundary_digest,
        unit.inventory_fingerprint,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        "applied",
    )


def _image(unit: RepairUnit, fence: RepairFence) -> RepairRollbackImage:
    return RepairRollbackImage(
        unit.run_id,
        unit.unit_id,
        f"image-{unit.unit_id}",
        unit.generation,
        unit.sequence,
        unit.attempt,
        fence.owner_id,
        fence.token,
        unit.boundary_digest,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        _DIGEST,
        "available",
    )


def _repository(transaction: _Transaction) -> CrmDealRepairIntegrationRepository:
    return CrmDealRepairIntegrationRepository(cast(Neo4jClient, _Client(transaction)))


def test_post_apply_unit_replay_and_exact_fence_read_bind_every_query_parameter() -> None:
    context = _context()
    request = _request("apply")
    unit = _unit()
    transaction = _Transaction({queries.READ_ALLOCATED_UNIT: [{"unit": asdict(unit)}]})
    repository = _repository(transaction)
    fence_id, fingerprint = repository._fence_identity(request, context, unit)
    fence = RepairFence(
        unit.run_id,
        unit.unit_id,
        fence_id,
        unit.generation,
        unit.sequence,
        unit.attempt,
        request.control.owner_id,
        request.control.token_digest,
        unit.boundary_digest,
        fingerprint,
        "claimed",
    )
    transaction._rows[queries.READ_FENCE] = [{"unit": asdict(unit), "fence": asdict(fence)}]

    assert repository.allocated_unit(request, context) == unit
    assert repository.read_fence(request, context) == (unit, fence)
    assert [query for query, _ in transaction.calls] == [
        queries.READ_ALLOCATED_UNIT,
        queries.READ_FENCE,
    ]


def test_status_and_rollback_share_the_persisted_stable_authorization_transition() -> None:
    context = _context()
    unit = _unit()
    fence = _fence(unit)
    mutation = _mutation(unit, fence)
    image = _image(unit, fence)
    transaction = _Transaction({})
    repository = _repository(transaction)
    status_request = _request("rollback-status")
    transition_id = repository._authorization_transition_id(
        unit, fence, _DIGEST, status_request, "reviewed_rollback_v1"
    )
    authorization = {"authorization_transition_id": transition_id}
    transaction._rows[queries.CREATE_AND_READ_ROLLBACK_AUTHORIZATION] = [
        {
            "unit": asdict(unit),
            "fence": asdict(fence),
            "result": asdict(mutation),
            "image": asdict(image),
            "authorization": authorization,
        },
        {
            "unit": asdict(unit),
            "fence": asdict(fence),
            "result": asdict(mutation),
            "image": asdict(image),
            "authorization": authorization,
        },
    ]
    status = repository.create_rollback_authorization(
        status_request, context, unit, fence, _DIGEST, "reviewed_rollback_v1"
    )
    rollback = repository.create_rollback_authorization(
        _request("rollback"), context, unit, fence, _DIGEST, "reviewed_rollback_v1"
    )

    assert status.authorization_transition_id == authorization["authorization_transition_id"]
    assert rollback.authorization_transition_id == status.authorization_transition_id


def test_release_authority_binds_request_digest_for_exact_post_release_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context()
    request = _request("release-dispatch", unit_id=None)
    completion = {
        "completion_id": context.authority.completion_id,
        "overlay_digest": context.authority.overlay_digest,
        "allocation_digest": context.authority.allocation_digest,
        "unit_set_digest": context.authority.allocation_unit_set_digest,
        "request_digest": context.authority.allocation_request_digest,
        "allocation_origin_key_id": context.authority.allocation_origin_key_id,
        "allocation_origin_hmac": context.authority.allocation_origin_hmac,
        "receipt_digest": context.authority.allocation_receipt_digest,
        "allocation_revision": context.authority.allocation_revision,
        "unit_count": 0,
    }
    transaction = _Transaction(
        {
            queries.READ_RELEASE_AUTHORITY: [
                {"completion": completion, "sealed_boundary_digest": _DIGEST}
            ]
        }
    )
    repository = _repository(transaction)
    import src.graph.crm_deal_identity_repair_integration as module

    monkeypatch.setattr(module, "allocation_origin_hmac", lambda **_: "b" * 64)
    loaded = repository.load_authority(request, context.run, _DIGEST, "key", b"secret")

    assert loaded == context.authority
    assert transaction.calls[0][1]["request_digest"] == request.request_digest


def test_set_digests_are_independent_of_database_collection_order() -> None:
    context = _context()
    left = _unit("left", 0)
    right = _unit("right", 1)
    transaction = _Transaction(
        {
            queries.READ_RUN_SETS: [
                {
                    "unit_count": 2,
                    "units": [asdict(right), asdict(left)],
                    "fences": [asdict(_fence(right)), asdict(_fence(left))],
                },
                {
                    "unit_count": 2,
                    "units": [asdict(left), asdict(right)],
                    "fences": [asdict(_fence(left)), asdict(_fence(right))],
                },
            ]
        }
    )
    repository = _repository(transaction)
    context = RepairIntegrationContext(
        context.run,
        context.inventory,
        RepairIntegrationAuthority(
            context.authority.completion_id,
            context.authority.overlay_digest,
            context.authority.allocation_digest,
            object_digest(
                b"crm-deal-identity-repair-allocation-unit-set-v1\x00",
                {
                    "units": [
                        asdict(replace(left, state="allocated")),
                        asdict(replace(right, state="allocated")),
                    ]
                },
            ),
            context.authority.allocation_request_digest,
            context.authority.allocation_origin_key_id,
            context.authority.allocation_origin_hmac,
            context.authority.allocation_receipt_digest,
            context.authority.sealed_boundary_digest,
            context.authority.allocation_revision,
        ),
    )
    expected = repository._set_digests(cast(ManagedTransaction, transaction), context)
    context = RepairIntegrationContext(
        context.run,
        context.inventory,
        RepairIntegrationAuthority(
            context.authority.completion_id,
            context.authority.overlay_digest,
            context.authority.allocation_digest,
            expected[0],
            context.authority.allocation_request_digest,
            context.authority.allocation_origin_key_id,
            context.authority.allocation_origin_hmac,
            context.authority.allocation_receipt_digest,
            context.authority.sealed_boundary_digest,
            context.authority.allocation_revision,
        ),
    )
    assert repository._set_digests(cast(ManagedTransaction, transaction), context) == expected


class _ContextLedger:
    def __init__(self, run: RepairQualificationRun) -> None:
        self._run = run
        self.snapshot_calls = 0

    def get_qualification(self, repair_id: str) -> RepairQualificationRun | None:
        return self._run if repair_id == self._run.repair_id else None

    def snapshot(self, **_: object) -> object:
        self.snapshot_calls += 1
        raise AssertionError("post-apply command must not run the original-boundary snapshot")

    def source_record_pks(self, repair_id: str) -> tuple[str, ...]:
        assert repair_id == self._run.repair_id
        return ()


class _ContextIntegration:
    def __init__(
        self,
        authority: RepairIntegrationAuthority,
        *,
        drifted: bool = False,
        exact_execution: bool = True,
        any_execution: bool = False,
        next_unit_drifted: bool = False,
    ) -> None:
        self._authority = authority
        self._drifted = drifted
        self._exact_execution = exact_execution
        self._any_execution = any_execution
        self._next_unit_drifted = next_unit_drifted
        self.next_unit_checks = 0

    def load_authority(self, *_: object) -> RepairIntegrationAuthority:
        if self._drifted:
            raise RuntimeError("repair current allocation/control/dispatch authority rejected")
        return self._authority

    def has_execution_evidence(self, *_: object) -> bool:
        return self._exact_execution

    def has_any_execution_evidence(self, *_: object) -> bool:
        return self._any_execution

    def assert_next_unit_boundary(self, *_: object) -> None:
        self.next_unit_checks += 1
        if self._next_unit_drifted:
            raise RuntimeError("repair integration next-unit boundary drift detected")


class _Store:
    def __enter__(self) -> _Store:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def test_context_loader_allows_post_apply_verify_but_rejects_authority_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#309 graph changes must not re-run the pre-mutation snapshot gate."""
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(LOCK_EX=0, LOCK_UN=0, flock=lambda *_args, **_kwargs: None),
    )
    import src.crm_deal_identity_repair.integration_runtime as runtime

    context = _context()
    ledger = _ContextLedger(context.run)
    settings = cast(Settings, SimpleNamespace(crm_deal_identity_repair_approval_root="."))
    verified = SimpleNamespace(
        manifest=SimpleNamespace(provenance=SimpleNamespace(artifact_path="."))
    )
    monkeypatch.setattr(
        runtime,
        "verify_approval_overlay",
        lambda *_args, **_kwargs: SimpleNamespace(overlay_digest=_DIGEST),
    )
    monkeypatch.setattr(
        runtime, "assert_overlay_binds_qualification", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runtime, "repair_artifact_store_from_settings", lambda _: _Store())
    monkeypatch.setattr(
        runtime, "verify_qualified_repair_artifact", lambda *_args, **_kwargs: verified
    )
    monkeypatch.setattr(runtime, "_read_inventory", lambda _: ())

    apply_request = _request("apply")
    apply_loader = runtime._context_loader(
        apply_request,
        cast(object, ledger),
        cast(object, _ContextIntegration(context.authority)),
        settings,
        b"approval-secret",
        "key",
    )
    assert apply_loader(apply_request).authority == context.authority
    verify_request = _request("verify")
    verify_loader = runtime._context_loader(
        verify_request,
        cast(object, ledger),
        cast(object, _ContextIntegration(context.authority)),
        settings,
        b"approval-secret",
        "key",
    )
    assert verify_loader(verify_request).authority == context.authority
    assert ledger.snapshot_calls == 0

    drifted_loader = runtime._context_loader(
        verify_request,
        cast(object, ledger),
        cast(object, _ContextIntegration(context.authority, drifted=True)),
        settings,
        b"approval-secret",
        "key",
    )
    with pytest.raises(RuntimeError, match="authority rejected"):
        drifted_loader(verify_request)
    assert ledger.snapshot_calls == 0



def test_context_loader_checks_new_unit_after_other_unit_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first apply for unit B cannot inherit unit A's raw-snapshot exemption."""
    monkeypatch.setitem(
        sys.modules,
        "fcntl",
        SimpleNamespace(LOCK_EX=0, LOCK_UN=0, flock=lambda *_args, **_kwargs: None),
    )
    import src.crm_deal_identity_repair.integration_runtime as runtime

    context = _context()
    ledger = _ContextLedger(context.run)
    settings = cast(Settings, SimpleNamespace(crm_deal_identity_repair_approval_root="."))
    verified = SimpleNamespace(
        manifest=SimpleNamespace(provenance=SimpleNamespace(artifact_path="."))
    )
    monkeypatch.setattr(
        runtime,
        "verify_approval_overlay",
        lambda *_args, **_kwargs: SimpleNamespace(overlay_digest=_DIGEST),
    )
    monkeypatch.setattr(
        runtime, "assert_overlay_binds_qualification", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runtime, "repair_artifact_store_from_settings", lambda _: _Store())
    monkeypatch.setattr(
        runtime, "verify_qualified_repair_artifact", lambda *_args, **_kwargs: verified
    )
    monkeypatch.setattr(runtime, "_read_inventory", lambda _: ())
    first_unit = _ContextIntegration(
        context.authority, exact_execution=False, any_execution=True
    )
    request = _request("apply")
    loader = runtime._context_loader(
        request,
        cast(object, ledger),
        cast(object, first_unit),
        settings,
        b"approval-secret",
        "key",
    )
    assert loader(request).authority == context.authority
    assert first_unit.next_unit_checks == 1
    assert ledger.snapshot_calls == 0

    drifted = _ContextIntegration(
        context.authority,
        exact_execution=False,
        any_execution=True,
        next_unit_drifted=True,
    )
    drifted_loader = runtime._context_loader(
        request,
        cast(object, ledger),
        cast(object, drifted),
        settings,
        b"approval-secret",
        "key",
    )
    with pytest.raises(RuntimeError, match="next-unit boundary drift"):
        drifted_loader(request)
    assert drifted.next_unit_checks == 1


def test_release_write_requires_complete_authority_parameters() -> None:
    context = _context()
    request = _request("release-dispatch", unit_id=None)
    transaction = _Transaction(
        {queries.RELEASE_DISPATCH: [{"request_digest": request.request_digest}]}
    )
    repository = _repository(transaction)

    repository.release_dispatch(request, context, _DIGEST, _DIGEST)

    query, params = transaction.calls[0]
    assert query == queries.RELEASE_DISPATCH
    required_authority = {
        "completion_id",
        "manifest_json",
        "allocation_origin_hmac",
        "allocation_receipt_digest",
    }
    assert required_authority <= set(params)


def test_rollback_receipt_replay_validates_the_complete_receipt_digest() -> None:
    context = _context()
    request = _request("rollback-status")
    unit = _unit()
    fence = _fence(unit)
    mutation = _mutation(unit, fence)
    image = _image(unit, fence)
    authorization = RepairRollbackAuthorization(
        unit,
        fence,
        mutation,
        image,
        "review",
        _DIGEST,
        "predecessor",
        "reviewed_rollback_v1",
        "authorization",
    )
    transaction = _Transaction({})
    repository = _repository(transaction)
    receipt_id = repository._receipt_id(request, image.image_digest)
    receipt_digest = rollback_status_receipt_digest(
        run_id=unit.run_id,
        unit_id=unit.unit_id,
        receipt_id=receipt_id,
        fence_id=fence.fence_id,
        mutation_id=mutation.mutation_id,
        image_digest=image.image_digest,
        authorization_transition_id=authorization.authorization_transition_id,
        authorization_digest=authorization.digest,
        status_digest=_DIGEST,
        control_revision=request.control.expected_revision,
        allocation_revision=context.authority.allocation_revision,
        completion_id=context.authority.completion_id,
        generation=unit.generation,
        sequence=unit.sequence,
        attempt=unit.attempt,
    )
    transaction._rows[queries.STORE_ROLLBACK_RECEIPT] = [
        {"receipt_id": receipt_id, "receipt_digest": receipt_digest}
    ]

    repository.store_rollback_receipt(request, context, authorization, _DIGEST)
    assert transaction.calls[-1][1]["receipt_digest"] == receipt_digest


def test_valid_looking_corrupt_status_digest_fails_receipt_authentication() -> None:
    context = _context()
    request = _request("accept", unit_id=None)
    receipt = {
        "run_id": context.run.run_id,
        "unit_id": "unit",
        "receipt_id": "receipt",
        "fence_id": "fence",
        "mutation_id": "mutation",
        "image_digest": _DIGEST,
        "authorization_transition_id": "authorization",
        "authorization_digest": _DIGEST,
        "status_digest": _DIGEST,
        "control_revision": request.control.expected_revision,
        "allocation_revision": context.authority.allocation_revision,
        "completion_id": context.authority.completion_id,
        "generation": 1,
        "sequence": 0,
        "attempt": 1,
    }
    receipt["receipt_digest"] = rollback_status_receipt_digest(
        run_id=context.run.run_id,
        unit_id="unit",
        receipt_id="receipt",
        fence_id="fence",
        mutation_id="mutation",
        image_digest=_DIGEST,
        authorization_transition_id="authorization",
        authorization_digest=_DIGEST,
        status_digest=_DIGEST,
        control_revision=request.control.expected_revision,
        allocation_revision=context.authority.allocation_revision,
        completion_id=context.authority.completion_id,
        generation=1,
        sequence=0,
        attempt=1,
    )
    assert _validated_receipt_binding(receipt, request, context)["receipt_id"] == "receipt"
    receipt["status_digest"] = "sha256:" + "b" * 64
    with pytest.raises(RuntimeError, match="receipt digest differs"):
        _validated_receipt_binding(receipt, request, context)


def test_zero_unit_acceptance_binds_an_empty_receipt_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The receipt-binding subquery must retain its aggregate row for an empty set."""
    import src.graph.crm_deal_identity_repair_integration as module

    context = _context()
    request = _request("accept", unit_id=None)
    transaction = _Transaction({queries.READ_RUN_RECEIPTS: [{"unit_count": 0, "receipts": []}]})
    repository = _repository(transaction)
    monkeypatch.setattr(repository, "_set_digests", lambda *_: (_DIGEST, _DIGEST))
    monkeypatch.setattr(
        module,
        "read_run_equation",
        lambda *_: SimpleNamespace(balanced=True, digest=_DIGEST),
    )
    acceptance_digest = repository._acceptance_receipt_digest(
        request, context, _DIGEST, _DIGEST, _DIGEST
    )
    transaction._rows[queries.ACCEPT_AND_RELEASE] = [
        {"receipt_digest": acceptance_digest}
    ]
    command = cast(object, SimpleNamespace())
    repository.accept(request, context, cast(object, command))

    accept_params = transaction.calls[-1][1]
    assert accept_params["receipt_bindings"] == []
