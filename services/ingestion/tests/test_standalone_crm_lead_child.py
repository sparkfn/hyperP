"""Focused fake-based tests for fenced lead #302-to-#303 execution."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_openlines.models import CrmContact
from src.crm_company_contracts import CrmCompanyMembershipHead
from src.crm_company_membership_writer import (
    CrmCompanyMembershipCommitResult,
    CrmCompanyMembershipMutation,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
)
from src.standalone_crm_lead_child import StandaloneCrmLeadSourceHandler
from src.standalone_crm_source_child_runtime import StandaloneCrmSourceChildClaim
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactPage,
    StandaloneCrmSourceFactReceipt,
)
from src.standalone_crm_unit_repository import StandaloneCrmAtomicUnitCommit
from tests._standalone_crm_lane_a_fakes import lead_envelope

_OBSERVED_AT = datetime(2020, 1, 1, tzinfo=UTC)
_RECEIPT = StandaloneCrmSourceFactReceipt(
    6,
    "source-record-6",
    3,
    "record-hash-6",
    "2020-01-01T00:00:00Z",
)


@dataclass
class _LeadIo:
    rows: tuple[CrmContact, ...]
    page_intent: str | None = "lead-page-intent-5"
    next_calls: list[tuple[int, int]] = field(default_factory=list)

    def next_lead(self, cursor: int, frozen_upper_id: int) -> tuple[CrmContact, ...]:
        self.next_calls.append((cursor, frozen_upper_id))
        return self.rows

    def lead_page_intent_id(self, cursor: int) -> str:
        if self.page_intent is None:
            raise RuntimeError("completed lead page intent is missing")
        assert cursor == 5
        return self.page_intent


@dataclass
class _SourceFacts:
    result: StandaloneCrmSourceFactCommitResult
    pending_receipt: StandaloneCrmSourceFactReceipt | None = None
    pages: list[StandaloneCrmSourceFactPage] = field(default_factory=list)

    def write(self, page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactCommitResult:
        self.pages.append(page)
        return self.result

    def pending_lead_receipt(
        self, envelope: object, checkpoint: StandaloneCrmCheckpoint
    ) -> StandaloneCrmSourceFactReceipt | None:
        del envelope, checkpoint
        return self.pending_receipt


@dataclass
class _Memberships:
    decision: str = "committed"
    current_head: CrmCompanyMembershipHead | None = None
    commits: list[StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipMutation]] = field(
        default_factory=list
    )

    def current_membership_head(
        self, scope: object, subject_kind: str, subject_id: str
    ) -> CrmCompanyMembershipHead | None:
        del scope, subject_kind, subject_id
        return self.current_head

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipMutation],
    ) -> CrmCompanyMembershipCommitResult:
        self.commits.append(request)
        return CrmCompanyMembershipCommitResult(self.decision)


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("lead",),
        StandaloneCrmBudget(2, 10, 3600, 4, 20, 2, "2026-08-29T00:00:00Z"),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )


def _claim(*, positioned: bool = False) -> StandaloneCrmSourceChildClaim:
    checkpoint = StandaloneCrmCheckpoint(
        "census-a",
        "lead",
        10,
        None,
        5,
        6 if positioned else None,
        0 if positioned else None,
        0,
        0,
        1,
        2,
    )
    return StandaloneCrmSourceChildClaim(lead_envelope(), checkpoint, _request())


def _recovery_claim() -> StandaloneCrmSourceChildClaim:
    envelope = replace(lead_envelope(), last_committed_id=6)
    checkpoint = StandaloneCrmCheckpoint("census-a", "lead", 10, None, 6, None, None, 1, 0, 1, 2)
    return StandaloneCrmSourceChildClaim(envelope, checkpoint, _request())


def _lead(company_id: str | None = None) -> CrmContact:
    return CrmContact(
        "6",
        "Ada",
        kind="lead",
        observed_at=_OBSERVED_AT,
        company_id=company_id,
    )


def _source_result(
    decision: str = "committed",
    *,
    processed_rows: int = 1,
    receipts: tuple[StandaloneCrmSourceFactReceipt, ...] = (_RECEIPT,),
    lead_company_id: str | None = None,
) -> StandaloneCrmSourceFactCommitResult:
    if decision in {"committed", "replayed"}:
        if lead_company_id is not None:
            receipts = (
                StandaloneCrmSourceFactReceipt(
                    _RECEIPT.row_id,
                    _RECEIPT.source_record_pk,
                    _RECEIPT.source_record_version,
                    _RECEIPT.record_hash,
                    _RECEIPT.observed_at,
                    lead_company_id,
                ),
            )
        return StandaloneCrmSourceFactCommitResult(
            decision,
            processed_rows,
            0,
            0,
            receipts,
        )
    return StandaloneCrmSourceFactCommitResult(decision)


def _handler(
    source_facts: _SourceFacts,
    memberships: _Memberships,
) -> StandaloneCrmLeadSourceHandler:
    return StandaloneCrmLeadSourceHandler(source_facts, memberships)


@pytest.mark.parametrize(("company_id", "binding_count"), ((None, 0), ("303", 1)))
def test_lead_handler_commits_source_fact_then_complete_zero_or_one_membership(
    company_id: str | None,
    binding_count: int,
) -> None:
    source_facts = _SourceFacts(_source_result(lead_company_id=company_id))
    memberships = _Memberships()

    result = _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead(company_id),)))

    assert result == "lead_completed"
    assert source_facts.pages[0].defer_contact_cursor is False
    assert source_facts.pages[0].rows == (_lead(company_id),)
    commit = memberships.commits[0]
    assert commit.expected_checkpoint.last_committed_id == 6
    assert commit.proposed_checkpoint.last_committed_id == 6
    assert commit.accounting_delta.processed_rows == 0
    assert len(commit.mutation.snapshot_record.membership_snapshot.bindings) == binding_count


def test_replayed_source_fact_and_idempotent_membership_converge_without_double_accounting() -> (
    None
):
    source_facts = _SourceFacts(_source_result("replayed"))
    memberships = _Memberships("idempotent")

    result = _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead("303"),)))

    assert result == "lead_completed"
    commit = memberships.commits[0]
    assert commit.accounting_delta.processed_rows == 0
    assert commit.expected_checkpoint.processed_rows == 1
    assert commit.proposed_checkpoint.processed_rows == 1


def test_malformed_lead_singleton_is_accounted_without_a_receipt_dependent_membership() -> None:
    source_facts = _SourceFacts(StandaloneCrmSourceFactCommitResult("committed", 1, 0, 1, ()))
    memberships = _Memberships()

    assert (
        _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead(),))) == "lead_completed"
    )
    assert len(source_facts.pages) == 1
    assert memberships.commits == []


def test_lead_pending_receipt_recovers_membership_before_any_later_source_fetch() -> None:
    receipt = StandaloneCrmSourceFactReceipt(
        _RECEIPT.row_id,
        _RECEIPT.source_record_pk,
        _RECEIPT.source_record_version,
        _RECEIPT.record_hash,
        _RECEIPT.observed_at,
        "303",
    )
    source_facts = _SourceFacts(_source_result(), pending_receipt=receipt)
    memberships = _Memberships()
    client = _LeadIo(())

    assert _handler(source_facts, memberships).run(_recovery_claim(), client) == "lead_completed"
    assert client.next_calls == []
    assert len(memberships.commits) == 1


def test_completed_lead_handoff_does_not_loop_and_fetches_only_after_head_verification() -> None:
    receipt = StandaloneCrmSourceFactReceipt(
        _RECEIPT.row_id,
        _RECEIPT.source_record_pk,
        _RECEIPT.source_record_version,
        _RECEIPT.record_hash,
        _RECEIPT.observed_at,
        "303",
    )
    first = _Memberships()
    source_facts = _SourceFacts(_source_result(), pending_receipt=receipt)
    _handler(source_facts, first).run(_recovery_claim(), _LeadIo(()))
    verified = _Memberships(current_head=first.commits[0].mutation.compare_and_set.proposed_head)
    client = _LeadIo(())

    assert _handler(source_facts, verified).run(_recovery_claim(), client) == "no_lead_row"
    assert client.next_calls == [(6, 10)]
    assert verified.commits == []


def test_lead_membership_cas_uses_the_exact_current_scoped_head() -> None:
    first = _Memberships()
    _handler(_SourceFacts(_source_result()), first).run(_claim(), _LeadIo((_lead(),)))
    current = first.commits[0].mutation.compare_and_set.proposed_head
    later = _Memberships(current_head=current)
    claim = _claim()
    claim = replace(
        claim,
        envelope=replace(
            claim.envelope,
            availability=replace(claim.envelope.availability, available_at="2026-08-29T00:00:00Z"),
        ),
    )
    receipt = StandaloneCrmSourceFactReceipt(
        6, "source-record-6-later", 4, "record-hash-6-later", _RECEIPT.observed_at
    )

    _handler(
        _SourceFacts(StandaloneCrmSourceFactCommitResult("committed", 1, 0, 0, (receipt,))),
        later,
    ).run(claim, _LeadIo((_lead(),)))

    assert later.commits[0].mutation.compare_and_set.expected_head == current


@pytest.mark.parametrize(
    "decision",
    ("conflict", "authority_rejected", "attempt_exhausted", "occurrence_exhausted"),
)
def test_source_fact_failure_decisions_stop_before_membership(decision: str) -> None:
    source_facts = _SourceFacts(_source_result(decision))
    memberships = _Memberships()

    assert _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead(),))) == decision
    assert memberships.commits == []


@pytest.mark.parametrize(
    "decision",
    ("stale_or_conflict", "authority_rejected", "attempt_exhausted", "occurrence_exhausted"),
)
def test_membership_failure_decisions_are_returned_without_a_second_source_write(
    decision: str,
) -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships(decision)

    assert _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead(),))) == decision
    assert len(source_facts.pages) == 1


def test_missing_completed_page_intent_fails_closed_before_source_fact_commit() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()

    with pytest.raises(RuntimeError, match="completed lead page intent"):
        _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead(),), None))
    assert source_facts.pages == []
    assert memberships.commits == []


def test_lead_handler_rejects_missing_or_mismatched_durable_source_receipt() -> None:
    source_facts = _SourceFacts(_source_result(receipts=()))
    memberships = _Memberships()

    with pytest.raises(RuntimeError, match="one durable receipt"):
        _handler(source_facts, memberships).run(_claim(), _LeadIo((_lead(),)))
    assert memberships.commits == []

    wrong_receipt = StandaloneCrmSourceFactReceipt(
        7,
        "source-record-7",
        1,
        "record-hash-7",
        "2020-01-01T00:00:00Z",
    )
    source_facts = _SourceFacts(_source_result(receipts=(wrong_receipt,)))
    with pytest.raises(RuntimeError, match="does not match"):
        _handler(source_facts, _Memberships()).run(_claim(), _LeadIo((_lead(),)))


def test_lead_handler_rejects_invalid_replayed_row_accounting() -> None:
    source_facts = _SourceFacts(_source_result(processed_rows=0))

    with pytest.raises(RuntimeError, match="one-row accounting"):
        _handler(source_facts, _Memberships()).run(_claim(), _LeadIo((_lead(),)))


def test_lead_handler_rejects_multiple_rows_and_contact_binding_positions() -> None:
    source_facts = _SourceFacts(_source_result())

    with pytest.raises(RuntimeError, match="exactly one lead row"):
        _handler(source_facts, _Memberships()).run(
            _claim(),
            _LeadIo((_lead(), _lead())),
        )
    assert source_facts.pages == []

    with pytest.raises(RuntimeError, match="contact binding position"):
        _handler(source_facts, _Memberships()).run(_claim(positioned=True), _LeadIo((_lead(),)))


def test_empty_bounded_lead_page_has_no_source_or_membership_effect() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()

    assert _handler(source_facts, memberships).run(_claim(), _LeadIo(())) == "no_lead_row"
    assert source_facts.pages == []
    assert memberships.commits == []
