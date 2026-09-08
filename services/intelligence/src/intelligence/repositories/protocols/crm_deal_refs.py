"""Read-only CRM deal-reference repository contract."""

from __future__ import annotations

from typing import Protocol, TypedDict


class DealReferenceRow(TypedDict):
    """Raw bounded graph row; raw payload remains in-memory only."""

    source_record_id: object
    source_record_version: object
    source_record_pk: object
    record_hash: object
    source_entity_type: object
    source_entity_id: object
    identity_policy_version: object
    record_entity_key: object
    owned_entity_keys: object
    owned_entity_count: object
    stage_id: object
    observed_at: object
    ingested_at: object
    lifecycle_status: object
    link_status: object
    raw_payload: object


class IdentityRevisionRow(TypedDict):
    """Raw immutable identity-revision row."""

    event_id: object
    global_revision: object
    source_instance_id: object
    source_entity_id: object
    identity_policy_version: object
    link_status: object
    hyperp_person_id: object
    person_status: object
    resolution_kind: object
    resolution_revision: object
    effective_at: object
    created_at: object


class IdentityBoundary(TypedDict):
    """Read-only identity stream counter and baseline readiness observation."""

    current_revision: int
    baseline_ready: bool


class CrmDealRefsRepository(Protocol):
    """Neo4j read contract needed to seal and reconcile one snapshot boundary."""

    def validate_source_instance(self, source_instance_id: str) -> None:
        """Reject a source instance that is not currently registered and active."""

    def close(self) -> None:
        """Release any child-process-local graph connection."""

    def read_identity_boundary(self) -> IdentityBoundary:
        """Read identity readiness and revision ceiling without graph mutation."""

    def list_deal_references(
        self, source_instance_id: str, as_of: str, page_size: int, max_records: int
    ) -> list[DealReferenceRow]:
        """Return every in-bound source version through deterministic keyset reads."""

    def list_identity_revisions(
        self,
        source_instance_id: str,
        source_entity_ids: tuple[str, ...],
        as_of: str,
        through_revision: int,
        page_size: int,
        max_records: int,
    ) -> list[IdentityRevisionRow]:
        """Return relevant immutable identity history through the sealed revision ceiling."""
