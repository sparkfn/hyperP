"""Pure domain serialization, fingerprint, and ordering helpers for CRM deal snapshots."""

from __future__ import annotations

from dataclasses import asdict

from intelligence.crm_deal_refs.models import (
    IDENTITY_POLICY_VERSION,
    QUERY_VERSION,
    SCHEMA_VERSION,
    SOURCE_SYSTEM,
    Boundary,
    Checkpoint,
    DealReference,
    IdentityRevision,
    PageManifest,
    canonical_digest,
    json_value,
)


def boundary(
    source: str,
    as_of: str,
    capture: str,
    page: int,
    maximum: int,
    ceiling: int,
    deals: tuple[DealReference, ...],
    identities: tuple[IdentityRevision, ...],
) -> Boundary:
    return Boundary(
        SCHEMA_VERSION,
        QUERY_VERSION,
        SOURCE_SYSTEM,
        source,
        IDENTITY_POLICY_VERSION,
        as_of,
        capture,
        page,
        maximum,
        ceiling,
        len(deals),
        None if not deals else deals[-1].key,
        canonical_digest([asdict(item.key) for item in deals]),
        canonical_digest([immutable_deal(item) for item in deals]),
        canonical_digest([immutable_identity(item) for item in identities]),
        canonical_digest(
            [deal_observation(item) for item in deals]
            + [identity_observation(item) for item in identities]
        ),
    )


def snapshot(
    boundary: Boundary, checkpoint: Checkpoint, manifests: list[PageManifest]
) -> dict[str, object]:
    digest = canonical_digest(json_value(boundary))
    values = [json_value(item) for item in manifests]
    return {
        "schema_version": SCHEMA_VERSION,
        "boundary_sha256": digest,
        "source_system": boundary.source_system,
        "source_instance_id": boundary.source_instance_id,
        "identity_policy_version": boundary.identity_policy_version,
        "as_of": boundary.as_of,
        "deal_page_count": checkpoint.deal_pages,
        "identity_page_count": checkpoint.identity_pages,
        "deal_record_count": checkpoint.deal_records,
        "identity_record_count": checkpoint.identity_records,
        "page_manifests_sha256": canonical_digest(values),
        "page_manifests": values,
    }


def fingerprints(value: Boundary) -> tuple[object, ...]:
    return (
        value.source_membership_count,
        value.source_membership_sha256,
        value.immutable_facts_sha256,
        value.identity_facts_sha256,
        value.mutable_observations_sha256,
    )


def ordered_deals(items: tuple[DealReference, ...]) -> None:
    if [item.key for item in items] != sorted(item.key for item in items) or len(
        {item.key for item in items}
    ) != len(items):
        raise ValueError("deal selection is unordered")


def ordered_identities(
    items: tuple[IdentityRevision, ...], ceiling: int, sources: tuple[str, ...]
) -> None:
    revisions = [item.global_revision for item in items]
    if (
        revisions != sorted(revisions)
        or len(revisions) != len(set(revisions))
        or any(
            item.global_revision > ceiling or item.source_entity_id not in sources for item in items
        )
    ):
        raise ValueError("identity selection is invalid")


def immutable_deal(item: DealReference) -> dict[str, object]:
    return {
        key: value
        for key, value in asdict(item).items()
        if key
        not in {"lifecycle_status_observed", "link_status_observed", "observation_captured_at"}
    }


def immutable_identity(item: IdentityRevision) -> dict[str, object]:
    return {
        key: value
        for key, value in asdict(item).items()
        if key not in {"person_status_observed", "person_observation_captured_at"}
    }


def deal_observation(item: DealReference) -> dict[str, object]:
    return {
        "key": asdict(item.key),
        "lifecycle_status_observed": item.lifecycle_status_observed,
        "link_status_observed": item.link_status_observed,
    }


def identity_observation(item: IdentityRevision) -> dict[str, object]:
    return {
        "global_revision": item.global_revision,
        "person_status_observed": item.person_status_observed,
    }


def value(item: object) -> dict[str, object]:
    raw = json_value(item)
    if not isinstance(raw, dict):
        raise RuntimeError("record serialization failed")
    return {str(key): entry for key, entry in raw.items()}


def pages[T](items: tuple[T, ...], size: int) -> tuple[tuple[T, ...], ...]:
    return tuple(tuple(items[index : index + size]) for index in range(0, len(items), size))
