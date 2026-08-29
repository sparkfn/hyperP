"""Fenced one-lead #302-to-#303 source-child execution."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from src.connectors.bitrix_openlines.models import CrmContact
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
from src.crm_identity_associations import CrmCompanyMembershipSnapshot, lead_membership_snapshot
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import (
    LeadSourceChildEnvelope,
    StandaloneCrmSourceChildScope,
)
from src.standalone_crm_source_child_runtime import StandaloneCrmSourceChildClaim
from src.standalone_crm_source_fact_models import (
    StandaloneCrmSourceFactCommitResult,
    StandaloneCrmSourceFactPage,
    StandaloneCrmSourceFactReceipt,
)
from src.standalone_crm_unit_repository import (
    StandaloneCrmAtomicUnitCommit,
    StandaloneCrmUnitAccountingDelta,
)


class LeadSourceChildIo(Protocol):
    """Reservation-backed source operations for one bounded lead page."""

    def next_lead(self, cursor: int, frozen_upper_id: int) -> tuple[CrmContact, ...]: ...

    def lead_page_intent_id(self, cursor: int) -> str: ...


class LeadSourceFacts(Protocol):
    def write(self, page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactCommitResult: ...

    def pending_lead_receipt(
        self, envelope: LeadSourceChildEnvelope, checkpoint: StandaloneCrmCheckpoint
    ) -> StandaloneCrmSourceFactReceipt | None: ...


class LeadMembershipCommitter(Protocol):
    def current_membership_head(
        self, scope: StandaloneCrmSourceChildScope, subject_kind: str, subject_id: str
    ) -> CrmCompanyMembershipHead | None: ...

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipMutation],
    ) -> CrmCompanyMembershipCommitResult: ...


class StandaloneCrmLeadSourceHandler:
    """Persist one lead fact then its complete zero-or-one membership snapshot."""

    def __init__(
        self,
        source_facts: LeadSourceFacts,
        memberships: LeadMembershipCommitter,
    ) -> None:
        self._source_facts = source_facts
        self._memberships = memberships

    def run(self, claim: StandaloneCrmSourceChildClaim, client: LeadSourceChildIo) -> str:
        envelope = claim.envelope
        if not isinstance(envelope, LeadSourceChildEnvelope):
            raise RuntimeError("lead handler received a non-lead source authority")
        _require_unpositioned_checkpoint(claim.checkpoint)
        recovered = self._source_facts.pending_lead_receipt(envelope, claim.checkpoint)
        if recovered is not None:
            snapshot = lead_membership_snapshot(
                lead_id=str(recovered.row_id), company_id=recovered.lead_company_id
            )
            current = self._memberships.current_membership_head(
                envelope.scope, "lead", snapshot.subject_id
            )
            if _head_matches_receipt(current, recovered, envelope.availability.available_at):
                return self._run_next(envelope, claim.checkpoint, client)
            return self._commit_recovered(envelope, claim.checkpoint, recovered, snapshot, current)
        return self._run_next(envelope, claim.checkpoint, client)

    def _run_next(
        self,
        envelope: LeadSourceChildEnvelope,
        checkpoint: StandaloneCrmCheckpoint,
        client: LeadSourceChildIo,
    ) -> str:
        rows = client.next_lead(envelope.last_committed_id, envelope.frozen_upper_id)
        if not rows:
            return "no_lead_row"
        if len(rows) != 1:
            raise RuntimeError("lead source child must receive exactly one lead row")
        row = rows[0]
        page = StandaloneCrmSourceFactPage(
            envelope,
            client.lead_page_intent_id(envelope.last_committed_id),
            envelope.last_committed_id,
            checkpoint,
            (row,),
        )
        source_result = self._source_facts.write(page)
        if source_result.decision not in {"committed", "replayed"}:
            return source_result.decision
        receipt = _exact_receipt(source_result, row.id)
        checkpoint = _source_fact_checkpoint(checkpoint, receipt, source_result)
        membership_envelope = replace(envelope, last_committed_id=receipt.row_id)
        snapshot = lead_membership_snapshot(lead_id=row.id, company_id=receipt.lead_company_id)
        current = self._memberships.current_membership_head(
            envelope.scope, "lead", snapshot.subject_id
        )
        membership_result = self._memberships.commit_unit(
            build_company_membership_commit(
                membership_envelope,
                _membership_mutation(membership_envelope, receipt, snapshot, current),
                checkpoint,
                checkpoint,
                StandaloneCrmUnitAccountingDelta(0, 0, 0),
            )
        )
        if membership_result.decision not in {"committed", "idempotent"}:
            return membership_result.decision
        return "lead_completed"

    def _commit_recovered(
        self,
        envelope: LeadSourceChildEnvelope,
        checkpoint: StandaloneCrmCheckpoint,
        receipt: StandaloneCrmSourceFactReceipt,
        snapshot: CrmCompanyMembershipSnapshot,
        current: CrmCompanyMembershipHead | None,
    ) -> str:
        result = self._memberships.commit_unit(
            build_company_membership_commit(
                envelope,
                _membership_mutation(envelope, receipt, snapshot, current),
                checkpoint,
                checkpoint,
                StandaloneCrmUnitAccountingDelta(0, 0, 0),
            )
        )
        return (
            "lead_completed" if result.decision in {"committed", "idempotent"} else result.decision
        )


def _require_unpositioned_checkpoint(checkpoint: StandaloneCrmCheckpoint) -> None:
    if checkpoint.binding_subject_id is not None or checkpoint.binding_offset is not None:
        raise RuntimeError("lead source child cannot execute a contact binding position")


def _exact_receipt(
    result: StandaloneCrmSourceFactCommitResult,
    lead_id: str,
) -> StandaloneCrmSourceFactReceipt:
    if len(result.receipts) != 1:
        raise RuntimeError("lead source-fact commit did not return one durable receipt")
    receipt = result.receipts[0]
    if receipt.row_id != int(lead_id):
        raise RuntimeError("lead source receipt does not match the fetched lead")
    if result.processed_rows != 1 or result.failed_rows != 0:
        raise RuntimeError("lead source-fact receipt has invalid one-row accounting")
    return receipt


def _source_fact_checkpoint(
    prior: StandaloneCrmCheckpoint,
    receipt: StandaloneCrmSourceFactReceipt,
    result: StandaloneCrmSourceFactCommitResult,
) -> StandaloneCrmCheckpoint:
    return replace(
        prior,
        last_committed_id=receipt.row_id,
        processed_rows=prior.processed_rows + result.processed_rows,
        skipped_rows=prior.skipped_rows + result.skipped_rows,
    )


def _membership_mutation(
    envelope: LeadSourceChildEnvelope,
    receipt: StandaloneCrmSourceFactReceipt,
    snapshot: CrmCompanyMembershipSnapshot,
    current_head: CrmCompanyMembershipHead | None,
) -> CrmCompanyMembershipMutation:
    record = CrmCompanyMembershipSnapshotRecord(
        envelope.scope,
        snapshot,
        f"bitrix-crm-lead-{snapshot.subject_id}",
        receipt.source_record_pk,
        receipt.source_record_version,
        receipt.record_hash,
        receipt.observed_at,
        envelope.availability,
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
    head = CrmCompanyMembershipHead(envelope.scope, "lead", snapshot.subject_id, record)
    return CrmCompanyMembershipMutation(
        record,
        observations,
        CrmCompanyMembershipHeadCompareAndSet(current_head, head),
    )


def _head_matches_receipt(
    head: CrmCompanyMembershipHead | None,
    receipt: StandaloneCrmSourceFactReceipt,
    available_at: str,
) -> bool:
    if head is None:
        return False
    record = head.snapshot_record
    return (
        record.source_record_pk == receipt.source_record_pk
        and record.source_record_version == receipt.source_record_version
        and record.source_record_hash == receipt.record_hash
        and record.observed_at == receipt.observed_at
        and record.availability.available_at == available_at
    )
