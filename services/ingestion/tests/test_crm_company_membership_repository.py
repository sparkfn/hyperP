"""Fake-transaction tests for the A-S2 Neo4j repository boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from neo4j import ManagedTransaction, Record
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload
from src.crm_company_contracts import (
    CrmCompanyMembershipHead,
    CrmCompanyMembershipHeadCompareAndSet,
    CrmCompanyMembershipObservation,
    CrmCompanyMembershipSnapshotRecord,
)
from src.crm_company_membership_writer import (
    CrmCompanyMembershipCommitResult,
    CrmCompanyMembershipMutation,
    build_company_membership_commit,
    membership_company_reference,
)
from src.crm_identity_associations import normalize_company_membership_snapshot
from src.graph.client import Neo4jClient
from src.graph.crm_company_membership import CrmCompanyMembershipRepository
from src.graph.queries.crm_company_membership import (
    CLAIM_DESCRIPTION_TRANSITION,
    CLAIM_MEMBERSHIP_TRANSITION,
    READ_CENSUS_REQUEST,
    UPSERT_COMPANY_REFERENCE,
    UPSERT_MEMBERSHIP_OBSERVATION,
    UPSERT_MEMBERSHIP_SNAPSHOT,
    VERIFY_COMPANY_REFERENCE,
    VERIFY_MEMBERSHIP_OBSERVATION,
    VERIFY_MEMBERSHIP_SNAPSHOT,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
    canonical_request_payload,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmUnitAccountingDelta,
)
from tests._standalone_crm_lane_a_fakes import (
    contact_envelope,
    source_availability,
    source_scope,
)


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
        request_json: str | object,
        decision: str | None,
        *,
        missing_query: str | None = None,
    ) -> None:
        self.request_json = request_json
        self.decision = decision
        self.missing_query = missing_query
        self.calls: list[tuple[str, dict[str, object]]] = []

    def run(self, query: str, **parameters: object) -> _Result:
        self.calls.append((query, parameters))
        if query == READ_CENSUS_REQUEST:
            return _Result(_Record({"request_json": self.request_json}))
        if query == CLAIM_MEMBERSHIP_TRANSITION:
            if self.decision is None:
                return _Result(None)
            return _Result(_Record({"decision": self.decision}))
        if query == self.missing_query:
            return _Result(None)
        return _Result(_Record({"persisted": True}))


class _Client:
    def __init__(self, tx: _Tx) -> None:
        self.tx = tx
        self.write_count = 0

    def execute_write(
        self,
        work: Callable[[ManagedTransaction], CrmCompanyMembershipCommitResult],
    ) -> CrmCompanyMembershipCommitResult:
        self.write_count += 1
        return work(cast(ManagedTransaction, self.tx))


def _stored_request(*, rows_per_attempt: int = 10) -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("contact",),
        StandaloneCrmBudget(
            2,
            rows_per_attempt,
            3600,
            4,
            20,
            2,
            "2026-08-29T00:00:00Z",
        ),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )


def _commit(
    *, nonempty: bool = False
) -> StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipMutation]:
    payloads = (CrmCompanyBindingPayload("3", 0, "7", "Y"),) if nonempty else ()
    snapshot = normalize_company_membership_snapshot(
        subject_type="contact",
        subject_id="5",
        payloads=payloads,
    )
    record = CrmCompanyMembershipSnapshotRecord(
        source_scope(),
        snapshot,
        "bitrix-crm-contact-5",
        "contact-record-5",
        1,
        "contact-hash-5",
        "2026-08-27T00:00:00Z",
        source_availability(),
        len(snapshot.bindings),
    )
    observations = tuple(
        CrmCompanyMembershipObservation(
            record,
            membership_company_reference(record, binding.company_id),
            binding.sort,
            binding.role_id,
            binding.is_primary,
        )
        for binding in snapshot.bindings
    )
    head = CrmCompanyMembershipHead(record.scope, "contact", "5", record)
    mutation = CrmCompanyMembershipMutation(
        record,
        observations,
        CrmCompanyMembershipHeadCompareAndSet(None, head),
    )
    expected = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 5, 0, 0, 0, 1, 2)
    proposed = StandaloneCrmCheckpoint("census-a", "contact", 10, None, 5, 5, 1, 1, 0, 1, 2)
    return build_company_membership_commit(
        contact_envelope(),
        mutation,
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )


def _repository(tx: _Tx) -> tuple[CrmCompanyMembershipRepository, _Client]:
    client = _Client(tx)
    return CrmCompanyMembershipRepository(cast(Neo4jClient, client)), client


def test_committed_empty_snapshot_uses_one_transaction_and_exact_query_order() -> None:
    request_json = canonical_request_payload(_stored_request())
    tx = _Tx(request_json, "committed")
    repository, client = _repository(tx)

    result = repository.commit_unit(_commit())

    assert result.decision == "committed"
    assert client.write_count == 1
    assert [query for query, _params in tx.calls] == [
        READ_CENSUS_REQUEST,
        CLAIM_MEMBERSHIP_TRANSITION,
        UPSERT_MEMBERSHIP_SNAPSHOT,
    ]
    claim = tx.calls[1][1]
    assert claim["request_json"] == request_json
    assert claim["expected_binding_subject"] == 5
    assert claim["proposed_binding_offset"] == 1
    assert claim["processed_delta"] == 1


def test_nonempty_commit_creates_reference_snapshot_and_observation_in_order() -> None:
    tx = _Tx(canonical_request_payload(_stored_request()), "committed")
    repository, _client = _repository(tx)

    repository.commit_unit(_commit(nonempty=True))

    assert [query for query, _params in tx.calls[2:]] == [
        UPSERT_COMPANY_REFERENCE,
        UPSERT_MEMBERSHIP_SNAPSHOT,
        UPSERT_MEMBERSHIP_OBSERVATION,
    ]


def test_idempotent_replay_verifies_only_and_cannot_double_account() -> None:
    tx = _Tx(canonical_request_payload(_stored_request()), "idempotent")
    repository, _client = _repository(tx)

    result = repository.commit_unit(_commit(nonempty=True))

    assert result.decision == "idempotent"
    assert [query for query, _params in tx.calls[2:]] == [
        VERIFY_COMPANY_REFERENCE,
        VERIFY_MEMBERSHIP_SNAPSHOT,
        VERIFY_MEMBERSHIP_OBSERVATION,
    ]


@pytest.mark.parametrize(
    "decision",
    ("stale_or_conflict", "attempt_exhausted", "occurrence_exhausted"),
)
def test_rejected_claim_never_runs_domain_queries(decision: str) -> None:
    tx = _Tx(canonical_request_payload(_stored_request()), decision)
    repository, _client = _repository(tx)

    result = repository.commit_unit(_commit())

    assert result.decision == decision
    assert len(tx.calls) == 2


def test_missing_authority_or_budget_mismatch_rejects_before_claim() -> None:
    mismatched = canonical_request_payload(_stored_request(rows_per_attempt=9))
    tx = _Tx(mismatched, "committed")
    repository, _client = _repository(tx)

    result = repository.commit_unit(_commit())

    assert result.decision == "authority_rejected"
    assert [query for query, _params in tx.calls] == [READ_CENSUS_REQUEST]


@pytest.mark.parametrize("request_json", ("not-json", "[]", 7))
def test_malformed_persisted_request_fails_closed(request_json: object) -> None:
    tx = _Tx(request_json, "committed")
    repository, _client = _repository(tx)

    with pytest.raises((RuntimeError, ValueError)):
        repository.commit_unit(_commit())
    assert len(tx.calls) == 1


def test_unknown_query_decision_fails_closed() -> None:
    tx = _Tx(canonical_request_payload(_stored_request()), "invented")
    repository, _client = _repository(tx)

    with pytest.raises(RuntimeError, match="unknown decision"):
        repository.commit_unit(_commit())


@pytest.mark.parametrize(
    "missing_query",
    (UPSERT_COMPANY_REFERENCE, UPSERT_MEMBERSHIP_SNAPSHOT, UPSERT_MEMBERSHIP_OBSERVATION),
)
def test_immutable_conflict_raises_to_roll_back_whole_transaction(missing_query: str) -> None:
    tx = _Tx(
        canonical_request_payload(_stored_request()),
        "committed",
        missing_query=missing_query,
    )
    repository, _client = _repository(tx)

    with pytest.raises(RuntimeError, match="immutable"):
        repository.commit_unit(_commit(nonempty=True))


def test_query_source_contains_no_forbidden_identity_or_second_budget_topology() -> None:
    query_text = "\n".join(
        (
            CLAIM_MEMBERSHIP_TRANSITION,
            UPSERT_COMPANY_REFERENCE,
            UPSERT_MEMBERSHIP_SNAPSHOT,
            UPSERT_MEMBERSHIP_OBSERVATION,
        )
    )
    for forbidden in (
        ":Person",
        ":Identifier",
        ":Entity",
        ":ReviewCase",
        ":Address",
        ":MatchDecision",
        ":MergeEvent",
        ":BudgetAuthorization",
        "HAS_FACT",
        "LINKED_TO",
    ):
        assert forbidden not in query_text


def test_claim_serializes_before_reading_head_and_checkpoint() -> None:
    lock = "SET census.crm_company_membership_lock = true"
    unlock = "REMOVE census.crm_company_membership_lock"
    for query in (CLAIM_DESCRIPTION_TRANSITION, CLAIM_MEMBERSHIP_TRANSITION):
        assert query.index(lock) < query.index("OPTIONAL MATCH (checkpoint")
        assert query.index(unlock) < query.index("OPTIONAL MATCH (checkpoint")
