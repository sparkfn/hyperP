"""A-S2 compile surface limited to company/membership and atomic contracts."""

from __future__ import annotations

from dataclasses import dataclass

from src.crm_company_contracts import CrmCompanyDescriptionHead, CrmCompanyMembershipHead
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import CompanySourceChildEnvelope
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmAtomicUnitRepository,
    StandaloneCrmUnitAccountingDelta,
)


@dataclass(frozen=True)
class CompanyMembershipMutation:
    description_head: CrmCompanyDescriptionHead
    membership_head: CrmCompanyMembershipHead


@dataclass(frozen=True)
class CompanyMembershipResult:
    committed: bool


class CompanyMembershipRepository(
    StandaloneCrmAtomicUnitRepository[CompanyMembershipMutation, CompanyMembershipResult]
):
    """Test-only A-S2 surface; it has no persistence or source-call behavior."""

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CompanyMembershipMutation],
    ) -> CompanyMembershipResult:
        mutation = request.mutation
        return CompanyMembershipResult(
            mutation.description_head.company_reference.scope == mutation.membership_head.scope
        )


def commit_company_membership(
    envelope: CompanySourceChildEnvelope,
    expected: StandaloneCrmCheckpoint,
    proposed: StandaloneCrmCheckpoint,
    description_head: CrmCompanyDescriptionHead,
    membership_head: CrmCompanyMembershipHead,
) -> bool:
    """Prove A-S2 consumes only company/membership and shared atomic contracts."""
    request = StandaloneCrmAtomicUnitCommit(
        envelope,
        CompanyMembershipMutation(description_head, membership_head),
        expected,
        proposed,
        StandaloneCrmUnitAccountingDelta(1, 0, 0),
    )
    return CompanyMembershipRepository().commit_unit(request).committed
