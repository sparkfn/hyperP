"""Focused fake-based tests for the fenced contact #302-to-#303 handoff."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload, CrmContact
from src.crm_company_contracts import CrmCompanyMembershipHead
from src.crm_company_membership_writer import CrmCompanyMembershipCommitResult
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
)
from src.standalone_crm_contact_child import StandaloneCrmContactSourceHandler
from src.standalone_crm_source_child_runtime import StandaloneCrmSourceChildClaim
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactPage,
    StandaloneCrmSourceFactReceipt,
)
from tests._standalone_crm_lane_a_fakes import contact_envelope

_OBSERVED_AT = datetime(2020, 1, 1, tzinfo=UTC)
_RECEIPT = StandaloneCrmSourceFactReceipt(
    6,
    "source-record-6",
    3,
    "record-hash-6",
    "2020-01-01T00:00:00Z",
)


@dataclass
class _ContactIo:
    rows: tuple[CrmContact, ...]
    bindings: tuple[CrmCompanyBindingPayload, ...] = ()
    page_intent: str | None = "page-intent-6"
    binding_intent: str | None = "binding-intent-6"
    next_calls: list[tuple[int, int]] = field(default_factory=list)
    binding_calls: list[str] = field(default_factory=list)

    def next_contact(self, cursor: int, frozen_upper_id: int) -> tuple[CrmContact, ...]:
        self.next_calls.append((cursor, frozen_upper_id))
        return self.rows

    def contact_page_intent_id(self, cursor: int) -> str:
        if self.page_intent is None:
            raise RuntimeError("completed page intent is missing")
        assert cursor == 5
        return self.page_intent

    def complete_company_bindings(self, contact_id: str) -> tuple[CrmCompanyBindingPayload, ...]:
        self.binding_calls.append(contact_id)
        return self.bindings

    def binding_intent_id(self, contact_id: int) -> str:
        if self.binding_intent is None:
            raise RuntimeError("completed binding intent is missing")
        assert contact_id == 6
        return self.binding_intent


@dataclass
class _SourceFacts:
    result: StandaloneCrmSourceFactCommitResult
    pending_receipt: StandaloneCrmSourceFactReceipt = _RECEIPT
    pages: list[StandaloneCrmSourceFactPage] = field(default_factory=list)
    pending_calls: list[tuple[object, int]] = field(default_factory=list)

    def write(self, page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactCommitResult:
        self.pages.append(page)
        return self.result

    def pending_contact_receipt(
        self,
        envelope: object,
        binding_subject_id: int,
    ) -> StandaloneCrmSourceFactReceipt:
        self.pending_calls.append((envelope, binding_subject_id))
        return self.pending_receipt


@dataclass
class _Memberships:
    decision: str = "committed"
    current_head: CrmCompanyMembershipHead | None = None
    commits: list[object] = field(default_factory=list)

    def current_membership_head(
        self, scope: object, subject_kind: str, subject_id: str
    ) -> CrmCompanyMembershipHead | None:
        del scope, subject_kind, subject_id
        return self.current_head

    def commit_unit(self, request: object) -> CrmCompanyMembershipCommitResult:
        self.commits.append(request)
        return CrmCompanyMembershipCommitResult(self.decision)


@dataclass
class _Closer:
    accepted: bool = True
    calls: list[tuple[object, ...]] = field(default_factory=list)

    def close_contact_binding_position(
        self,
        census_id: str,
        generation: int,
        fence_token: int,
        owner_id: str,
        task_name: str,
        task_id: str,
        payload_digest: str,
        frozen_upper_id: int,
        last_committed_id: int,
        contact_id: int,
        binding_count: int,
    ) -> bool:
        self.calls.append(
            (
                census_id,
                generation,
                fence_token,
                owner_id,
                task_name,
                task_id,
                payload_digest,
                frozen_upper_id,
                last_committed_id,
                contact_id,
                binding_count,
            )
        )
        return self.accepted


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("contact",),
        StandaloneCrmBudget(2, 10, 3600, 4, 20, 2, "2026-08-29T00:00:00Z"),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )


def _claim(*, pending: bool = False, binding_offset: int = 0) -> StandaloneCrmSourceChildClaim:
    envelope = contact_envelope()
    if not pending:
        envelope = replace(envelope, binding_subposition=None)
        checkpoint = StandaloneCrmCheckpoint(
            "census-a", "contact", 10, None, 5, None, None, 0, 0, 1, 2
        )
    else:
        position = envelope.binding_subposition
        assert position is not None
        envelope = replace(
            envelope,
            binding_subposition=replace(
                position,
                binding_subject_id=6,
                binding_offset=binding_offset,
            ),
        )
        checkpoint = StandaloneCrmCheckpoint(
            "census-a", "contact", 10, None, 5, 6, binding_offset, 1, 0, 1, 2
        )
    return StandaloneCrmSourceChildClaim(envelope, checkpoint, _request())


def _contact() -> CrmContact:
    return CrmContact("6", "Ada", kind="contact", observed_at=_OBSERVED_AT)


def _source_result(decision: str = "committed") -> StandaloneCrmSourceFactCommitResult:
    if decision in {"committed", "replayed"}:
        return StandaloneCrmSourceFactCommitResult(decision, 1, 0, 0, (_RECEIPT,))
    return StandaloneCrmSourceFactCommitResult(decision)


def _handler(
    source_facts: _SourceFacts,
    memberships: _Memberships,
    closer: _Closer,
) -> StandaloneCrmContactSourceHandler:
    return StandaloneCrmContactSourceHandler(source_facts, memberships, closer)


def test_contact_handler_commits_deferred_source_fact_membership_and_exact_close() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()
    closer = _Closer()
    client = _ContactIo((_contact(),), (CrmCompanyBindingPayload("303", 0, "7", True),))

    result = _handler(source_facts, memberships, closer).run(_claim(), client)

    assert result == "contact_completed"
    assert len(source_facts.pages) == 1
    assert source_facts.pages[0].defer_contact_cursor is True
    assert source_facts.pages[0].rows == (_contact(),)
    assert len(memberships.commits) == 1
    assert closer.calls[0][-3:] == (5, 6, 1)


def test_contact_handler_commits_an_empty_complete_membership_before_close() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()
    closer = _Closer()

    result = _handler(source_facts, memberships, closer).run(_claim(), _ContactIo((_contact(),)))

    assert result == "contact_completed"
    assert len(memberships.commits) == 1
    assert closer.calls[0][-1] == 0


def test_pending_contact_recovery_skips_source_page_and_resumes_membership_then_close() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()
    closer = _Closer()
    client = _ContactIo((), (CrmCompanyBindingPayload("303", 0, "7", True),))

    result = _handler(source_facts, memberships, closer).run(_claim(pending=True), client)

    assert result == "contact_completed"
    assert client.next_calls == []
    assert source_facts.pages == []
    assert len(source_facts.pending_calls) == 1
    assert len(memberships.commits) == 1
    assert closer.calls[0][-3:] == (5, 6, 1)


def test_pending_replay_after_membership_commit_skips_duplicate_membership_and_closes() -> None:
    source_facts = _SourceFacts(_source_result())
    initial = _Memberships()
    _handler(source_facts, initial, _Closer()).run(_claim(), _ContactIo((_contact(),)))
    commit = initial.commits[0]
    memberships = _Memberships(current_head=commit.mutation.compare_and_set.proposed_head)
    closer = _Closer()
    client = _ContactIo((), (CrmCompanyBindingPayload("303", 0, "7", True),))

    result = _handler(source_facts, memberships, closer).run(
        _claim(pending=True, binding_offset=1),
        client,
    )

    assert result == "contact_completed"
    assert memberships.commits == []
    assert client.binding_calls == []
    assert len(closer.calls) == 1


def test_pending_nonempty_membership_recovery_closes_without_refetching_changed_source() -> None:
    source_facts = _SourceFacts(_source_result())
    initial = _Memberships()
    bindings = (CrmCompanyBindingPayload("303", 0, "7", True),)
    _handler(source_facts, initial, _Closer()).run(_claim(), _ContactIo((_contact(),), bindings))
    committed = initial.commits[0].mutation.compare_and_set.proposed_head
    memberships = _Memberships(current_head=committed)
    client = _ContactIo((), (), binding_intent=None)
    closer = _Closer()

    result = _handler(source_facts, memberships, closer).run(
        _claim(pending=True, binding_offset=1),
        client,
    )

    assert result == "contact_completed"
    assert client.next_calls == []
    assert client.binding_calls == []
    assert memberships.commits == []
    assert closer.calls[0][-1] == 1


def test_contact_membership_cas_uses_the_exact_current_scoped_head() -> None:
    source_facts = _SourceFacts(_source_result())
    first = _Memberships()
    _handler(source_facts, first, _Closer()).run(_claim(), _ContactIo((_contact(),)))
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
        _Closer(),
    ).run(claim, _ContactIo((_contact(),)))

    assert later.commits[0].mutation.compare_and_set.expected_head == current


@pytest.mark.parametrize(
    "decision",
    ("conflict", "authority_rejected", "attempt_exhausted", "occurrence_exhausted"),
)
def test_source_fact_failure_decisions_stop_before_bindings_membership_or_close(
    decision: str,
) -> None:
    source_facts = _SourceFacts(_source_result(decision))
    memberships = _Memberships()
    closer = _Closer()
    client = _ContactIo((_contact(),))

    assert _handler(source_facts, memberships, closer).run(_claim(), client) == decision
    assert client.binding_calls == []
    assert memberships.commits == []
    assert closer.calls == []


@pytest.mark.parametrize(
    "decision",
    ("stale_or_conflict", "attempt_exhausted", "occurrence_exhausted", "authority_rejected"),
)
def test_membership_failure_decisions_stop_before_close(decision: str) -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships(decision)
    closer = _Closer()

    result = _handler(source_facts, memberships, closer).run(_claim(), _ContactIo((_contact(),)))

    assert result == decision
    assert closer.calls == []


def test_missing_completed_page_intent_stops_before_source_fact_commit() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()
    closer = _Closer()

    with pytest.raises(RuntimeError, match="completed page intent"):
        _handler(source_facts, memberships, closer).run(
            _claim(),
            _ContactIo((_contact(),), page_intent=None),
        )
    assert source_facts.pages == []
    assert memberships.commits == []
    assert closer.calls == []


def test_missing_completed_binding_intent_stops_before_membership_or_close() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()
    closer = _Closer()

    with pytest.raises(RuntimeError, match="completed binding intent"):
        _handler(source_facts, memberships, closer).run(
            _claim(),
            _ContactIo((_contact(),), binding_intent=None),
        )
    assert memberships.commits == []
    assert closer.calls == []


def test_close_cas_failure_is_reported_after_the_durable_membership_commit() -> None:
    source_facts = _SourceFacts(_source_result())
    memberships = _Memberships()
    closer = _Closer(False)

    result = _handler(source_facts, memberships, closer).run(_claim(), _ContactIo((_contact(),)))

    assert result == "close_rejected"
    assert len(memberships.commits) == 1
    assert len(closer.calls) == 1


def test_contact_handler_rejects_a_non_singleton_source_page() -> None:
    source_facts = _SourceFacts(_source_result())

    with pytest.raises(RuntimeError, match="exactly one contact row"):
        _handler(source_facts, _Memberships(), _Closer()).run(
            _claim(),
            _ContactIo((_contact(), _contact())),
        )
    assert source_facts.pages == []
