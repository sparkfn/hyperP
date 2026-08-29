"""Fenced one-contact #302 to #303 source-child execution."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from src.connectors.bitrix_openlines.models import CrmCompanyBindingPayload, CrmContact
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
from src.crm_identity_associations import (
    CrmCompanyMembershipSnapshot,
    normalize_company_membership_snapshot,
)
from src.standalone_crm_census_lifecycle import StandaloneCrmCheckpoint
from src.standalone_crm_child_contracts import (
    ContactBindingSubposition,
    ContactSourceChildEnvelope,
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


class ContactSourceChildIo(Protocol):
    def next_contact(self, cursor: int, frozen_upper_id: int) -> tuple[CrmContact, ...]: ...

    def contact_page_intent_id(self, cursor: int) -> str: ...

    def complete_company_bindings(
        self, contact_id: str
    ) -> tuple[CrmCompanyBindingPayload, ...]: ...

    def binding_intent_id(self, contact_id: int) -> str: ...


class ContactMembershipCommitter(Protocol):
    def current_membership_head(
        self, scope: StandaloneCrmSourceChildScope, subject_kind: str, subject_id: str
    ) -> CrmCompanyMembershipHead | None: ...

    def commit_unit(
        self,
        request: StandaloneCrmAtomicUnitCommit[CrmCompanyMembershipMutation],
    ) -> CrmCompanyMembershipCommitResult: ...


class ContactSourceFacts(Protocol):
    def write(self, page: StandaloneCrmSourceFactPage) -> StandaloneCrmSourceFactCommitResult: ...

    def pending_contact_receipt(
        self, envelope: ContactSourceChildEnvelope, binding_subject_id: int
    ) -> StandaloneCrmSourceFactReceipt: ...


class ContactCursorCloser(Protocol):
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
    ) -> bool: ...


class StandaloneCrmContactSourceHandler:
    """Processes exactly one contact and never advances its final cursor early."""

    def __init__(
        self,
        source_facts: ContactSourceFacts,
        memberships: ContactMembershipCommitter,
        closer: ContactCursorCloser,
    ) -> None:
        self._source_facts = source_facts
        self._memberships = memberships
        self._closer = closer

    def run(self, claim: StandaloneCrmSourceChildClaim, client: ContactSourceChildIo) -> str:
        envelope = claim.envelope
        if not isinstance(envelope, ContactSourceChildEnvelope):
            raise RuntimeError("contact handler received a non-contact source authority")
        if claim.checkpoint.binding_subject_id is None:
            rows = client.next_contact(envelope.last_committed_id, envelope.frozen_upper_id)
            if not rows:
                return "no_contact_row"
            if len(rows) != 1:
                raise RuntimeError("contact source child must receive exactly one contact row")
            row = rows[0]
            page = StandaloneCrmSourceFactPage(
                envelope,
                client.contact_page_intent_id(envelope.last_committed_id),
                envelope.last_committed_id,
                claim.checkpoint,
                (row,),
                True,
            )
            result = self._source_facts.write(page)
            if _is_malformed_singleton_completion(result):
                return "contact_completed"
            if result.decision not in {"committed", "replayed"} or len(result.receipts) != 1:
                return result.decision
            receipt = result.receipts[0]
            contact_id = row.id
            expected = replace(
                claim.checkpoint,
                binding_subject_id=receipt.row_id,
                binding_offset=0,
                processed_rows=claim.checkpoint.processed_rows + result.processed_rows,
                skipped_rows=claim.checkpoint.skipped_rows + result.skipped_rows,
            )
        else:
            contact_id = str(claim.checkpoint.binding_subject_id)
            receipt = self._source_facts.pending_contact_receipt(
                envelope,
                int(contact_id),
            )
            expected = claim.checkpoint
        if receipt.row_id != int(contact_id):
            raise RuntimeError("contact source receipt does not match its pending contact")
        current_head = self._memberships.current_membership_head(
            envelope.scope, "contact", contact_id
        )
        if (
            _receipt_head_binding_count(current_head, receipt, envelope.availability.available_at)
            is not None
        ):
            binding_count = _receipt_head_binding_count(
                current_head, receipt, envelope.availability.available_at
            )
            assert binding_count is not None
            return self._close(envelope, expected, receipt.row_id, binding_count)
        if _binding_offset(expected) != 0:
            raise RuntimeError("pending contact membership has no matching durable head")
        bindings = client.complete_company_bindings(contact_id)
        client.binding_intent_id(int(contact_id))
        positioned = replace(
            envelope,
            binding_subposition=ContactBindingSubposition(
                receipt.row_id,
                _binding_offset(expected),
            ),
        )
        snapshot = normalize_company_membership_snapshot(
            subject_type="contact", subject_id=contact_id, payloads=bindings
        )
        binding_count = len(snapshot.bindings)
        current_offset = _binding_offset(expected)
        if current_offset > binding_count:
            raise RuntimeError("contact binding position exceeds the complete binding snapshot")
        if current_offset not in {0, binding_count}:
            raise RuntimeError("contact binding position is not a complete membership boundary")
        if current_offset == 0:
            decision = self._commit_membership(
                positioned, receipt, snapshot, expected, current_head
            )
            if decision not in {"committed", "idempotent"}:
                return decision
        return self._close(envelope, expected, receipt.row_id, binding_count)

    def _close(
        self,
        envelope: ContactSourceChildEnvelope,
        expected: StandaloneCrmCheckpoint,
        contact_id: int,
        binding_count: int,
    ) -> str:
        if not self._closer.close_contact_binding_position(
            envelope.unit.census_id,
            envelope.unit.generation,
            envelope.unit.fence_token,
            envelope.unit.fence_owner_id,
            envelope.unit.task_name,
            envelope.unit.task_id,
            envelope.unit.payload_digest,
            envelope.frozen_upper_id,
            expected.last_committed_id,
            contact_id,
            binding_count,
        ):
            return "close_rejected"
        return "contact_completed"

    def _commit_membership(
        self,
        envelope: ContactSourceChildEnvelope,
        receipt: StandaloneCrmSourceFactReceipt,
        snapshot: CrmCompanyMembershipSnapshot,
        expected: StandaloneCrmCheckpoint,
        current_head: CrmCompanyMembershipHead | None,
    ) -> str:
        """Commit the complete snapshot exactly once from binding offset zero."""
        record = CrmCompanyMembershipSnapshotRecord(
            envelope.scope,
            snapshot,
            f"bitrix-crm-contact-{snapshot.subject_id}",
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
                membership_company_reference(record, item.company_id),
                item.sort,
                item.role_id,
                item.is_primary,
            )
            for item in snapshot.bindings
        )
        head = CrmCompanyMembershipHead(
            envelope.scope,
            "contact",
            snapshot.subject_id,
            record,
        )
        mutation = CrmCompanyMembershipMutation(
            record, observations, CrmCompanyMembershipHeadCompareAndSet(current_head, head)
        )
        proposed = replace(expected, binding_offset=len(snapshot.bindings))
        decision = self._memberships.commit_unit(
            build_company_membership_commit(
                envelope,
                mutation,
                expected,
                proposed,
                StandaloneCrmUnitAccountingDelta(0, 0, 0),
            )
        )
        return decision.decision


def _binding_offset(checkpoint: StandaloneCrmCheckpoint) -> int:
    if checkpoint.binding_subject_id is None or checkpoint.binding_offset is None:
        raise RuntimeError("contact membership requires a pending binding position")
    return checkpoint.binding_offset


def _receipt_head_binding_count(
    head: CrmCompanyMembershipHead | None,
    receipt: StandaloneCrmSourceFactReceipt,
    available_at: str,
) -> int | None:
    if head is None:
        return None
    record = head.snapshot_record
    if (
        record.source_record_pk != receipt.source_record_pk
        or record.source_record_version != receipt.source_record_version
        or record.source_record_hash != receipt.record_hash
        or record.observed_at != receipt.observed_at
        or record.availability.available_at != available_at
    ):
        return None
    return record.binding_count


def _is_malformed_singleton_completion(result: StandaloneCrmSourceFactCommitResult) -> bool:
    """A malformed row is accounted by #302 and deliberately has no #303 handoff."""
    return (
        result.decision in {"committed", "replayed"}
        and result.processed_rows == 1
        and result.failed_rows == 1
        and result.receipts == ()
    )
