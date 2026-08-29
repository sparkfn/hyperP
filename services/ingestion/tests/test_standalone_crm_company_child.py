"""Focused fake-based tests for fenced company #303 source-child execution."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import pytest
from src.connectors.bitrix_openlines.models import CrmCompany
from src.crm_company_contracts import CrmCompanyDescriptionHead
from src.crm_company_membership_writer import (
    CrmCompanyDescriptionMutation,
    CrmCompanyMembershipCommitResult,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_census_requests import (
    SourceSyncAuthority,
    SourceSyncCensusRequest,
    StandaloneCrmBudget,
)
from src.standalone_crm_company_child import StandaloneCrmCompanySourceHandler
from src.standalone_crm_source_child_runtime import StandaloneCrmSourceChildClaim
from src.standalone_crm_unit_repository import StandaloneCrmAtomicUnitCommit
from tests._standalone_crm_lane_a_fakes import company_envelope

_OBSERVED_AT = datetime(2020, 1, 1, tzinfo=UTC)


@dataclass
class _CompanyIo:
    rows: tuple[object, ...]
    page_intent: str | None = "company-page-intent-5"
    next_calls: list[tuple[int, int]] = field(default_factory=list)

    def next_company(self, cursor: int, frozen_upper_id: int) -> tuple[CrmCompany, ...]:
        self.next_calls.append((cursor, frozen_upper_id))
        return self.rows

    def company_page_intent_id(self, cursor: int) -> str:
        if self.page_intent is None:
            raise RuntimeError("completed company page intent is missing")
        assert cursor == 5
        return self.page_intent


@dataclass
class _Descriptions:
    decision: str = "committed"
    current_head: CrmCompanyDescriptionHead | None = None
    commits: list[StandaloneCrmAtomicUnitCommit[CrmCompanyDescriptionMutation]] = field(
        default_factory=list
    )

    def current_description_head(
        self, scope: object, company_id: str
    ) -> CrmCompanyDescriptionHead | None:
        del scope, company_id
        return self.current_head

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CrmCompanyDescriptionMutation],
    ) -> CrmCompanyMembershipCommitResult:
        self.commits.append(request)
        return CrmCompanyMembershipCommitResult(self.decision)


def _request() -> SourceSyncCensusRequest:
    return SourceSyncCensusRequest(
        "bitrix_chat",
        "portal-a",
        "control-a",
        "occurrence-a",
        ("company",),
        StandaloneCrmBudget(2, 10, 3600, 4, 20, 2, "2026-08-29T00:00:00Z"),
        "policy-a",
        "association-a",
        "configuration-a",
        SourceSyncAuthority("mapping", "mapping-digest", "projection", "projection-digest"),
    )


def _claim(*, positioned: bool = False) -> StandaloneCrmSourceChildClaim:
    checkpoint = StandaloneCrmCheckpoint(
        "census-a",
        "company",
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
    return StandaloneCrmSourceChildClaim(company_envelope(), checkpoint, _request())


def _company(title: str | None = "Northwind") -> CrmCompany:
    return CrmCompany("6", title, _OBSERVED_AT)


def _handler(descriptions: _Descriptions) -> StandaloneCrmCompanySourceHandler:
    return StandaloneCrmCompanySourceHandler(descriptions)


@pytest.mark.parametrize("title", (None, "", "Northwind"))
def test_company_handler_preserves_title_and_commits_reference_description_and_checkpoint(
    title: str | None,
) -> None:
    descriptions = _Descriptions()

    result = _handler(descriptions).run(_claim(), _CompanyIo((_company(title),)))

    assert result == "company_completed"
    commit = descriptions.commits[0]
    observation = commit.mutation.observation
    assert observation.company_title == title
    assert observation.source_record_version == 1
    assert observation.observed_at == "2020-01-01T00:00:00Z"
    assert observation.availability.available_at == "2026-08-28T00:00:00Z"
    assert commit.expected_checkpoint.last_committed_id == 5
    assert commit.proposed_checkpoint.last_committed_id == 6
    assert commit.proposed_checkpoint.processed_rows == 1
    assert commit.accounting_delta.processed_rows == 1


def test_company_duplicate_delivery_converges_on_idempotent_exact_mutation() -> None:
    descriptions = _Descriptions("idempotent")

    result = _handler(descriptions).run(_claim(), _CompanyIo((_company(),)))

    assert result == "company_completed"
    assert len(descriptions.commits) == 1
    assert descriptions.commits[0].accounting_delta.processed_rows == 1


def test_company_description_cas_uses_the_exact_current_scoped_head() -> None:
    first = _Descriptions()
    _handler(first).run(_claim(), _CompanyIo((_company(),)))
    current = first.commits[0].mutation.compare_and_set.proposed_head
    later = _Descriptions(current_head=current)
    claim = _claim()
    claim = replace(
        claim,
        envelope=replace(
            claim.envelope,
            availability=replace(claim.envelope.availability, available_at="2026-08-29T00:00:00Z"),
        ),
    )

    _handler(later).run(claim, _CompanyIo((_company("Changed"),)))

    assert later.commits[0].mutation.compare_and_set.expected_head == current


@pytest.mark.parametrize(
    "decision",
    ("stale_or_conflict", "authority_rejected", "attempt_exhausted", "occurrence_exhausted"),
)
def test_company_mutation_failures_are_returned_without_another_effect(decision: str) -> None:
    descriptions = _Descriptions(decision)

    assert _handler(descriptions).run(_claim(), _CompanyIo((_company(),))) == decision
    assert len(descriptions.commits) == 1


def test_company_missing_completed_intent_fails_closed_before_mutation() -> None:
    descriptions = _Descriptions()

    with pytest.raises(RuntimeError, match="completed company page intent"):
        _handler(descriptions).run(_claim(), _CompanyIo((_company(),), None))
    assert descriptions.commits == []


def test_company_handler_rejects_multirow_malformed_and_contact_position_responses() -> None:
    descriptions = _Descriptions()

    with pytest.raises(RuntimeError, match="exactly one company row"):
        _handler(descriptions).run(_claim(), _CompanyIo((_company(), _company())))
    assert descriptions.commits == []

    with pytest.raises(RuntimeError, match="malformed company row"):
        _handler(descriptions).run(_claim(), _CompanyIo((object(),)))
    assert descriptions.commits == []

    with pytest.raises(RuntimeError, match="contact binding position"):
        _handler(descriptions).run(_claim(positioned=True), _CompanyIo((_company(),)))
    assert descriptions.commits == []


def test_company_handler_rejects_blank_completed_intent_and_timezone_naive_observation() -> None:
    descriptions = _Descriptions()

    with pytest.raises(RuntimeError, match="durable successful reservation"):
        _handler(descriptions).run(_claim(), _CompanyIo((_company(),), ""))
    assert descriptions.commits == []

    naive = CrmCompany("6", "Northwind", datetime(2020, 1, 1))
    with pytest.raises(RuntimeError, match="timezone-naive"):
        _handler(descriptions).run(_claim(), _CompanyIo((naive,)))
    assert descriptions.commits == []


def test_empty_bounded_company_page_returns_no_work_without_mutation() -> None:
    descriptions = _Descriptions()

    assert _handler(descriptions).run(_claim(), _CompanyIo(())) == "no_company_row"
    assert descriptions.commits == []
