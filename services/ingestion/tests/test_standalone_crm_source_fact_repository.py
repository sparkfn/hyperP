"""Executable fake-transaction tests for #302 source-fact atomic commits."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import cast

import pytest
from neo4j import ManagedTransaction, Record
from src.connectors.bitrix_openlines.models import CrmContact
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_source_facts import (
    CLAIM_PAGE,
    FINALIZE_PAGE,
    READ_CENSUS_REQUEST,
    READ_PENDING_CONTACT_RECEIPT,
    READ_PENDING_LEAD_RECEIPT,
    READ_SOURCE_RECORD_RECEIPT,
    RESOLVE_COMMITTED_RECEIPT,
    STAMP_SOURCE_FACT_LINEAGE,
)
from src.graph.standalone_crm_source_fact_repository import (
    SourceFactPipelineAdapter,
    StandaloneCrmSourceFactRepository,
)
from src.models import IngestResult
from src.record_lifecycle import DuplicateVersion, PlannedVersion
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_child_contracts import (
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
)
from src.standalone_crm_source_fact_mapper import map_source_fact_page
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactPage,
    build_source_fact_commit,
)
from tests._standalone_crm_lane_a_fakes import contact_envelope, lead_envelope

_OBSERVED_AT = datetime(2020, 1, 1, tzinfo=UTC)


def _lead(identifier: str, full_name: str | None = "Ada") -> CrmContact:
    return CrmContact(identifier, full_name, kind="lead", observed_at=_OBSERVED_AT)


@dataclass(frozen=True)
class _Record:
    values: dict[str, object]

    def __getitem__(self, key: str) -> object:
        return self.values[key]


class _Result:
    def __init__(self, record: _Record | None) -> None:
        self._record = record

    def single(self) -> Record | None:
        return cast(Record | None, self._record)


class _Tx:
    def __init__(
        self,
        request_json: object,
        claim: str | None = "apply",
        *,
        receipt: str = "absent",
        final: bool = True,
    ) -> None:
        self.request_json = request_json
        self.claim = claim
        self.receipt = receipt
        self.final = final
        self.pending_record: _Record | None = None
        self.pending_lead_record: _Record | None = None
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        if query == RESOLVE_COMMITTED_RECEIPT:
            return _Result(
                _Record(
                    {
                        "decision": self.receipt,
                        "source_receipts_json": "[]",
                        "processed_rows": 0,
                        "skipped_rows": 0,
                        "failed_rows": 0,
                    }
                )
            )
        if query == READ_CENSUS_REQUEST:
            return _Result(_Record({"request_json": self.request_json}))
        if query == CLAIM_PAGE:
            return _Result(None if self.claim is None else _Record({"decision": self.claim}))
        if query == FINALIZE_PAGE:
            return _Result(_Record({"receipt_key": "receipt"}) if self.final else None)
        if query == STAMP_SOURCE_FACT_LINEAGE:
            return _Result(_Record({"source_record_pk": parameters["source_record_pk"]}))
        if query == READ_SOURCE_RECORD_RECEIPT:
            pk = parameters["source_record_pk"]
            return _Result(
                _Record(
                    {
                        "source_record_pk": pk,
                        "source_record_version": 1,
                        "record_hash": f"hash-{pk}",
                    }
                )
            )
        if query == READ_PENDING_CONTACT_RECEIPT:
            return _Result(self.pending_record)
        if query == READ_PENDING_LEAD_RECEIPT:
            return _Result(self.pending_lead_record)
        raise AssertionError("unexpected query")


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx
        self.writes = 0
        self.reads = 0

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        self.writes += 1
        return work(cast(ManagedTransaction, self.tx))

    def execute_read(self, work: Callable[[ManagedTransaction], object]) -> object:
        self.reads += 1
        return work(cast(ManagedTransaction, self.tx))


class _Adapter(SourceFactPipelineAdapter):
    def __init__(self, outcomes: list[DuplicateVersion | PlannedVersion]) -> None:
        self.outcomes = outcomes
        self.events: list[str] = []
        self.index = 0

    def plan(self, tx: ManagedTransaction, row: object) -> DuplicateVersion | PlannedVersion:
        del tx, row
        self.events.append("plan")
        outcome = self.outcomes[self.index]
        self.index += 1
        return outcome

    def persist(self, tx: ManagedTransaction, row: object, plan: PlannedVersion) -> IngestResult:
        del tx, row, plan
        self.events.append("persist")
        return IngestResult(source_record_id="id", source_record_pk=f"pk-{len(self.events)}")


def _stored_request(*, rows: int = 10) -> str:
    request = SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("lead",),
        StandaloneCrmBudget(2, rows, 3600, 4, 20, 2, "2026-08-29T00:00:00Z"),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )
    return canonical_request_payload(request)


def _request(rows: tuple[CrmContact, ...] | None = None) -> object:
    rows = rows or (_lead("6"),)
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, None, None, 0, 0, 1, 2)
    page = StandaloneCrmSourceFactPage(lead_envelope(), "call-a", 5, checkpoint, rows)
    return build_source_fact_commit(map_source_fact_page(page), skipped_rows=0)


def _planned() -> PlannedVersion:
    return PlannedVersion(1, None, (), None)


def _repository(
    tx: _Tx,
    adapter: _Adapter,
    failpoint: Callable[[str], None] | None = None,
) -> tuple[StandaloneCrmSourceFactRepository, _Client]:
    client = _Client(tx)
    return (
        StandaloneCrmSourceFactRepository(
            cast(Neo4jClient, client), adapter=adapter, failpoint=failpoint
        ),
        client,
    )


def test_success_plans_every_row_before_first_persist_and_commits_exact_delta() -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([_planned(), _planned()])
    repository, client = _repository(
        tx,
        adapter,
    )

    result = repository.commit_unit(_request((_lead("6"), _lead("7", "Bea"))))

    assert result.decision == "committed"
    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (2, 0, 0)
    assert adapter.events == ["plan", "plan", "persist", "persist"]
    assert client.writes == 1
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["proposed_cursor"] == 7
    assert final["proposed_processed"] == 2
    assert [item.row_id for item in result.receipts] == [6, 7]
    assert result.receipts[0].source_record_version == 1


def test_duplicate_page_row_skips_domain_persist_but_advances_atomic_accounting() -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([DuplicateVersion("existing"), _planned()])
    repository, _client = _repository(tx, adapter)

    result = repository.commit_unit(_request((_lead("6"), _lead("7", "Bea"))))

    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (2, 1, 0)
    assert adapter.events == ["plan", "plan", "persist"]
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["skipped_delta"] == 1 and final["proposed_skipped"] == 1


def test_one_contact_page_can_atomically_commit_receipt_and_pending_binding_position() -> None:
    checkpoint = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, None, None, 0, 0, 1, 2)
    page = StandaloneCrmSourceFactPage(
        replace(contact_envelope(), binding_subposition=None),
        "call-a",
        5,
        checkpoint,
        (CrmContact("6", "Ada", kind="contact", observed_at=_OBSERVED_AT),),
        True,
    )

    commit = build_source_fact_commit(map_source_fact_page(page), skipped_rows=0)

    assert commit.proposed_checkpoint.last_committed_id == 5
    assert (
        commit.proposed_checkpoint.binding_subject_id,
        commit.proposed_checkpoint.binding_offset,
    ) == (6, 0)
    assert commit.accounting_delta.processed_rows == 1


def test_deferred_contact_cursor_rejects_lead_and_multirow_pages() -> None:
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 5, None, None, 0, 0, 1, 2)
    with pytest.raises(ValueError, match="exactly one contact"):
        StandaloneCrmSourceFactPage(lead_envelope(), "call-a", 5, checkpoint, (_lead("6"),), True)


def test_exact_replay_is_a_typed_noop_without_adapter_calls() -> None:
    tx = _Tx(_stored_request(), receipt="replayed")
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    result = repository.commit_unit(_request())

    assert result.decision == "replayed"
    assert result.receipts == ()
    assert adapter.events == []
    assert [query for query, _ in tx.calls] == [RESOLVE_COMMITTED_RECEIPT]


def test_replay_restores_the_durable_exact_source_fact_receipt() -> None:
    tx = _Tx(_stored_request(), receipt="replayed")
    tx.receipt_json = (
        '[{"observed_at":"2020-01-01T00:00:00Z","record_hash":"hash-pk-6",'
        '"row_id":6,"source_record_pk":"pk-6","source_record_version":3}]'
    )
    original_run = tx.run

    def run(query: str, **parameters: object) -> _Result:
        result = original_run(query, **parameters)
        if query == RESOLVE_COMMITTED_RECEIPT:
            return _Result(
                _Record(
                    {
                        "decision": "replayed",
                        "source_receipts_json": tx.receipt_json,
                        "processed_rows": 1,
                        "skipped_rows": 0,
                        "failed_rows": 0,
                    }
                )
            )
        return result

    tx.run = run  # type: ignore[method-assign]
    repository, _client = _repository(tx, _Adapter([_planned()]))

    result = repository.commit_unit(_request())

    assert result.receipts[0].source_record_pk == "pk-6"
    assert result.receipts[0].source_record_version == 3
    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (1, 0, 0)


def test_receipt_conflict_precedes_mutable_request_authority() -> None:
    tx = _Tx("malformed", receipt="conflict")
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    assert repository.commit_unit(_request()).decision == "conflict"
    assert adapter.events == []
    assert [query for query, _ in tx.calls] == [RESOLVE_COMMITTED_RECEIPT]


@pytest.mark.parametrize(
    "decision", ("conflict", "authority_rejected", "attempt_exhausted", "occurrence_exhausted")
)
def test_claim_rejections_happen_before_any_plan_or_persist(decision: str) -> None:
    tx = _Tx(_stored_request(), decision)
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    assert repository.commit_unit(_request()).decision == decision
    assert adapter.events == []


def test_late_plan_conflict_prevents_all_persists() -> None:
    class _ConflictAdapter(_Adapter):
        def plan(self, tx: ManagedTransaction, row: object) -> DuplicateVersion | PlannedVersion:
            if self.index == 1:
                raise RuntimeError("late planning conflict")
            return super().plan(tx, row)

    tx = _Tx(_stored_request())
    adapter = _ConflictAdapter([_planned(), _planned()])
    repository, _client = _repository(tx, adapter)

    with pytest.raises(RuntimeError, match="late planning"):
        repository.commit_unit(_request((_lead("6"), _lead("7", "Bea"))))
    assert adapter.events == ["plan"]
    assert all(query != STAMP_SOURCE_FACT_LINEAGE for query, _ in tx.calls)


@pytest.mark.parametrize("boundary", ("after_planning", "after_domain_writes", "after_final_cas"))
def test_failpoints_escape_managed_transaction_at_each_atomic_boundary(boundary: str) -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([_planned()])

    def fail(name: str) -> None:
        if name == boundary:
            raise RuntimeError(name)

    repository, _client = _repository(tx, adapter, fail)
    with pytest.raises(RuntimeError, match=boundary):
        repository.commit_unit(_request())


def test_final_cas_denial_raises_after_domain_writes_so_driver_rolls_back() -> None:
    tx = _Tx(_stored_request(), final=False)
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    with pytest.raises(RuntimeError, match="final checkpoint CAS"):
        repository.commit_unit(_request())
    assert adapter.events == ["plan", "persist"]


@pytest.mark.parametrize("bad", ("not-json", "[]", 9))
def test_malformed_persisted_request_fails_closed_before_claim(bad: object) -> None:
    tx = _Tx(bad)
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    with pytest.raises(RuntimeError, match="persisted standalone CRM request"):
        repository.commit_unit(_request())
    assert adapter.events == []


def test_request_budget_mismatch_rejects_before_claim() -> None:
    tx = _Tx(_stored_request(rows=9))
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    assert repository.commit_unit(_request()).decision == "authority_rejected"
    assert [query for query, _ in tx.calls] == [RESOLVE_COMMITTED_RECEIPT, READ_CENSUS_REQUEST]


def test_timestampless_authorized_row_advances_failed_checkpoint_without_adapter_work() -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([])
    repository, _client = _repository(tx, adapter)
    request = _request((CrmContact("6", "Ada", kind="lead"),))

    result = repository.commit_unit(request)

    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (1, 0, 1)
    assert adapter.events == []
    assert all(query != STAMP_SOURCE_FACT_LINEAGE for query, _ in tx.calls)
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["failed_delta"] == 1
    assert final["proposed_cursor"] == 6
    assert final["proposed_processed"] == 1


def test_malformed_authorized_row_counts_failed_and_never_persists() -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([])
    repository, _client = _repository(tx, adapter)
    request = _request((_lead("6", ""),))

    result = repository.commit_unit(request)

    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (1, 0, 1)
    assert adapter.events == []
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["failed_delta"] == 1


def test_queries_have_single_source_binding_match_and_full_replay_fence() -> None:
    from src.graph.queries.standalone_crm_source_facts import (
        CLAIM_PAGE,
        FINALIZE_PAGE,
        RESOLVE_COMMITTED_RECEIPT,
    )

    assert CLAIM_PAGE.count("MATCH (:BitrixSourceInstance") == 1
    assert CLAIM_PAGE.count("MATCH (:BitrixExecutionSourceBinding") == 1
    for term in (
        "authorization_digest",
        "fence_owner_id",
        "payload_digest",
        "availability_contract_version",
        "call_intent_id",
        "attempt.call_count",
        "census.occurrence_calls",
    ):
        assert term in CLAIM_PAGE and term in FINALIZE_PAGE
    for term in (
        "authorization_digest",
        "fence_owner_id",
        "payload_digest",
        "availability_contract_version",
        "call_intent_id",
    ):
        assert term in RESOLVE_COMMITTED_RECEIPT


def _pending_contact_receipt_json(*, row_id: int = 6) -> str:
    return json.dumps(
        [
            {
                "row_id": row_id,
                "source_record_pk": "source-record-6",
                "source_record_version": 3,
                "record_hash": "record-hash-6",
                "observed_at": "2020-01-01T00:00:00Z",
            }
        ]
    )


def _pending_contact_envelope() -> ContactSourceChildEnvelope:
    return replace(
        contact_envelope(),
        binding_subposition=ContactBindingSubposition(6, 0),
    )


def test_pending_contact_receipt_returns_the_exact_durable_deferred_receipt() -> None:
    tx = _Tx(_stored_request())
    tx.pending_record = _Record(
        {"receipt_count": 1, "source_receipts_json": _pending_contact_receipt_json()}
    )
    repository, client = _repository(tx, _Adapter([]))
    envelope = _pending_contact_envelope()

    receipt = repository.pending_contact_receipt(envelope, 6)

    assert receipt.source_record_pk == "source-record-6"
    assert receipt.source_record_version == 3
    assert client.reads == 1 and client.writes == 0
    query, parameters = tx.calls[-1]
    assert query == READ_PENDING_CONTACT_RECEIPT
    assert parameters == {
        "census_id": "census-a",
        "generation": 1,
        "fence_token": 2,
        "fence_owner_id": "worker-a",
        "source_key": "bitrix_chat",
        "source_instance_id": "portal-a",
        "control_instance_id": "control-a",
        "task_name": "source.child",
        "task_id": "contact-task",
        "payload_digest": "sha256:" + "a" * 64,
        "frozen_upper_id": 10,
        "binding_subject_id": 6,
    }


@pytest.mark.parametrize(
    ("record", "message"),
    (
        (None, "missing or ambiguous"),
        (_Record({"receipt_count": 2, "source_receipts_json": "[]"}), "missing or ambiguous"),
        (_Record({"receipt_count": 1, "source_receipts_json": "not-json"}), "malformed"),
        (_Record({"receipt_count": 1, "source_receipts_json": "{}"}), "malformed"),
        (
            _Record(
                {
                    "receipt_count": 1,
                    "source_receipts_json": _pending_contact_receipt_json(row_id=7),
                }
            ),
            "malformed",
        ),
    ),
)
def test_pending_contact_receipt_fails_closed_for_missing_ambiguous_or_malformed_receipts(
    record: _Record | None,
    message: str,
) -> None:
    tx = _Tx(_stored_request())
    tx.pending_record = record
    repository, _client = _repository(tx, _Adapter([]))

    with pytest.raises(RuntimeError, match=message):
        repository.pending_contact_receipt(_pending_contact_envelope(), 6)


def test_pending_contact_receipt_requires_the_exact_parent_issued_subject_position() -> None:
    tx = _Tx(_stored_request())
    repository, _client = _repository(tx, _Adapter([]))

    with pytest.raises(ValueError, match="exact binding position"):
        repository.pending_contact_receipt(_pending_contact_envelope(), 7)


def test_pending_receipt_query_reasserts_current_fence_and_publication_identity() -> None:
    for term in (
        "census_id: $census_id",
        "generation: $generation",
        "fence_token: $fence_token",
        "owner_id: $fence_owner_id",
        "source_key: $source_key",
        "source_instance_id: $source_instance_id",
        "control_instance_id: $control_instance_id",
        "task_name: $task_name",
        "task_id: $task_id",
        "payload_digest: $payload_digest",
        "frozen_upper_id: $frozen_upper_id",
        "pending_binding_subject_id: $binding_subject_id",
        "status: 'committed'",
    ):
        assert term in READ_PENDING_CONTACT_RECEIPT
    assert (
        "fence_token: $fence_token"
        not in READ_PENDING_CONTACT_RECEIPT.split("MATCH (checkpoint", 1)[0]
    )
    assert "MATCH (:StandaloneCrmChildPublication" in READ_PENDING_CONTACT_RECEIPT
    assert "MATCH (:StandaloneCrmChildPublication" in READ_PENDING_LEAD_RECEIPT


def test_pending_lead_receipt_recovers_the_exact_durable_company_snapshot_input() -> None:
    tx = _Tx(_stored_request())
    tx.pending_lead_record = _Record(
        {
            "receipt_count": 1,
            "source_receipts_json": json.dumps(
                [
                    {
                        "row_id": 6,
                        "source_record_pk": "source-record-6",
                        "source_record_version": 3,
                        "record_hash": "record-hash-6",
                        "observed_at": "2020-01-01T00:00:00Z",
                        "lead_company_id": "303",
                    }
                ]
            ),
        }
    )
    repository, client = _repository(tx, _Adapter([]))
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 6, None, None, 1, 0, 1, 2)

    receipt = repository.pending_lead_receipt(lead_envelope(), checkpoint)

    assert receipt is not None and receipt.lead_company_id == "303"
    assert client.reads == 1 and client.writes == 0
    assert tx.calls[-1][0] == READ_PENDING_LEAD_RECEIPT
    assert "proposed_cursor: $last_committed_id" in READ_PENDING_LEAD_RECEIPT
