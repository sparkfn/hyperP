"""Fenced one-company #303 source-child execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC
from typing import Protocol

from src.connectors.bitrix_openlines.models import CrmCompany
from src.crm_company_contracts import (
    CrmCompanyDescriptionHead,
    CrmCompanyDescriptionHeadCompareAndSet,
    CrmCompanyDescriptionObservation,
    CrmCompanyReference,
)
from src.crm_company_membership_writer import (
    CrmCompanyDescriptionMutation,
    CrmCompanyMembershipCommitResult,
    build_company_description_commit,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import (
    CompanySourceChildEnvelope,
    StandaloneCrmSourceChildScope,
)
from src.standalone_crm_source_child_runtime import StandaloneCrmSourceChildClaim
from src.standalone_crm_source_fact_models import strict_row_id
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmUnitAccountingDelta,
)


class CompanySourceChildIo(Protocol):
    """Reservation-backed source operations for one bounded company page."""

    def next_company(self, cursor: int, frozen_upper_id: int) -> tuple[CrmCompany, ...]: ...

    def company_page_intent_id(self, cursor: int) -> str: ...


class CompanyDescriptionCommitter(Protocol):
    def current_description_head(
        self, scope: StandaloneCrmSourceChildScope, company_id: str
    ) -> CrmCompanyDescriptionHead | None: ...

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CrmCompanyDescriptionMutation],
    ) -> CrmCompanyMembershipCommitResult: ...


class StandaloneCrmCompanySourceHandler:
    """Persist one complete company reference and TITLE observation atomically."""

    def __init__(self, descriptions: CompanyDescriptionCommitter) -> None:
        self._descriptions = descriptions

    def run(self, claim: StandaloneCrmSourceChildClaim, client: CompanySourceChildIo) -> str:
        envelope = claim.envelope
        if not isinstance(envelope, CompanySourceChildEnvelope):
            raise RuntimeError("company handler received a non-company source authority")
        _require_unpositioned_checkpoint(claim.checkpoint)
        rows = client.next_company(envelope.last_committed_id, envelope.frozen_upper_id)
        if not rows:
            return "no_company_row"
        if len(rows) != 1:
            raise RuntimeError("company source child must receive exactly one company row")
        company = rows[0]
        if not isinstance(company, CrmCompany):
            raise RuntimeError("company source child returned a malformed company row")
        company_id = strict_row_id(company.id)
        if company.title is not None and not isinstance(company.title, str):
            raise RuntimeError("company source child returned a malformed TITLE")
        _require_completed_intent(client.company_page_intent_id(envelope.last_committed_id))
        expected = claim.checkpoint
        proposed = replace(
            expected,
            last_committed_id=company_id,
            processed_rows=expected.processed_rows + 1,
        )
        result = self._descriptions.commit_unit(
            build_company_description_commit(
                envelope,
                _description_mutation(
                    envelope,
                    company,
                    self._descriptions.current_description_head(envelope.scope, str(company_id)),
                ),
                expected,
                proposed,
                StandaloneCrmUnitAccountingDelta(1, 0, 0),
            )
        )
        if result.decision not in {"committed", "idempotent"}:
            return result.decision
        return "company_completed"


def _require_unpositioned_checkpoint(checkpoint: StandaloneCrmCheckpoint) -> None:
    if checkpoint.binding_subject_id is not None or checkpoint.binding_offset is not None:
        raise RuntimeError("company source child cannot execute a contact binding position")


def _require_completed_intent(intent_id: str) -> None:
    if not intent_id.strip():
        raise RuntimeError("company source call has no durable successful reservation receipt")


def _description_mutation(
    envelope: CompanySourceChildEnvelope,
    company: CrmCompany,
    current_head: CrmCompanyDescriptionHead | None,
) -> CrmCompanyDescriptionMutation:
    company_id = strict_row_id(company.id)
    identifier = str(company_id)
    reference = CrmCompanyReference(
        envelope.scope,
        identifier,
        f"bitrix-crm-company-{identifier}",
    )
    observation = CrmCompanyDescriptionObservation(
        reference,
        _source_record_pk(envelope, identifier),
        1,
        _source_record_hash(envelope, company),
        company.title,
        _observed_at(company),
        envelope.availability,
    )
    head = CrmCompanyDescriptionHead(reference, observation)
    return CrmCompanyDescriptionMutation(
        observation,
        CrmCompanyDescriptionHeadCompareAndSet(current_head, head),
    )


def _source_record_pk(envelope: CompanySourceChildEnvelope, company_id: str) -> str:
    return ":".join(
        (
            "standalone-crm-company",
            envelope.scope.source_instance_id,
            envelope.scope.control_instance_id,
            company_id,
        )
    )


def _source_record_hash(envelope: CompanySourceChildEnvelope, company: CrmCompany) -> str:
    payload = {
        "source_record_id": f"bitrix-crm-company-{company.id}",
        "source_instance_id": envelope.scope.source_instance_id,
        "company_id": company.id,
        "title": company.title,
        "observed_at": _observed_at(company),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _observed_at(company: CrmCompany) -> str | None:
    if company.observed_at is None:
        return None
    if company.observed_at.tzinfo is None:
        raise RuntimeError("company source child returned a timezone-naive observation")
    return company.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
