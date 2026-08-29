"""Executable fake-transaction tests for #302 source-fact atomic commits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import ManagedTransaction, Record
from src.connectors.bitrix_openlines.models import CrmContact
from src.graph.client import Neo4jClient
from src.graph.queries.standalone_crm_source_facts import (
    CLAIM_PAGE,
    FINALIZE_PAGE,
    READ_CENSUS_REQUEST,
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
from src.standalone_crm_source_fact_mapper import map_source_fact_page
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactPage,
    build_source_fact_commit,
)
from tests._standalone_crm_lane_a_fakes import lead_envelope


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
        self, request_json: object, claim: str | None = "apply", *, final: bool = True
    ) -> None:
        self.request_json = request_json
        self.claim = claim
        self.final = final
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        if query == READ_CENSUS_REQUEST:
            return _Result(_Record({"request_json": self.request_json}))
        if query == CLAIM_PAGE:
            return _Result(None if self.claim is None else _Record({"decision": self.claim}))
        if query == FINALIZE_PAGE:
            return _Result(_Record({"receipt_key": "receipt"}) if self.final else None)
        if query == STAMP_SOURCE_FACT_LINEAGE:
            return _Result(_Record({"source_record_pk": parameters["source_record_pk"]}))
        raise AssertionError("unexpected query")


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx
        self.writes = 0

    def execute_write(self, work: Callable[[ManagedTransaction], object]) -> object:
        self.writes += 1
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
    rows = rows or (CrmContact("6", "Ada", kind="lead"),)
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

    result = repository.commit_unit(
        _request((CrmContact("6", "Ada", kind="lead"), CrmContact("7", "Bea", kind="lead")))
    )

    assert result.decision == "committed"
    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (2, 0, 0)
    assert adapter.events == ["plan", "plan", "persist", "persist"]
    assert client.writes == 1
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["proposed_cursor"] == 7
    assert final["proposed_processed"] == 2


def test_duplicate_page_row_skips_domain_persist_but_advances_atomic_accounting() -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([DuplicateVersion("existing"), _planned()])
    repository, _client = _repository(tx, adapter)

    result = repository.commit_unit(
        _request((CrmContact("6", "Ada", kind="lead"), CrmContact("7", "Bea", kind="lead")))
    )

    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (2, 1, 0)
    assert adapter.events == ["plan", "plan", "persist"]
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["skipped_delta"] == 1 and final["proposed_skipped"] == 1


def test_exact_replay_is_a_typed_noop_without_adapter_calls() -> None:
    tx = _Tx(_stored_request(), "replayed")
    adapter = _Adapter([_planned()])
    repository, _client = _repository(tx, adapter)

    result = repository.commit_unit(_request())

    assert result.decision == "replayed"
    assert adapter.events == []
    assert [query for query, _ in tx.calls] == [READ_CENSUS_REQUEST, CLAIM_PAGE]


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
        repository.commit_unit(
            _request((CrmContact("6", "Ada", kind="lead"), CrmContact("7", "Bea", kind="lead")))
        )
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
    assert [query for query, _ in tx.calls] == [READ_CENSUS_REQUEST]


def test_malformed_authorized_row_counts_failed_and_never_persists() -> None:
    tx = _Tx(_stored_request())
    adapter = _Adapter([])
    repository, _client = _repository(tx, adapter)
    request = _request((CrmContact("6", "", kind="lead"),))

    result = repository.commit_unit(request)

    assert (result.processed_rows, result.skipped_rows, result.failed_rows) == (1, 0, 1)
    assert adapter.events == []
    final = next(parameters for query, parameters in tx.calls if query == FINALIZE_PAGE)
    assert final["failed_delta"] == 1


def test_queries_have_single_source_binding_match_and_full_replay_fence() -> None:
    from src.graph.queries.standalone_crm_source_facts import CLAIM_PAGE, FINALIZE_PAGE

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
