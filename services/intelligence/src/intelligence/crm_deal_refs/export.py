"""Bounded snapshot capture and write orchestration; validation lives in snapshot_validation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm_deal_refs.checkpoints import (
    copy_regular_file,
    replace_json,
    write_new_json,
    write_new_page,
)
from intelligence.crm_deal_refs.export_support import (
    boundary as _boundary,
)
from intelligence.crm_deal_refs.export_support import (
    fingerprints as _fingerprints,
)
from intelligence.crm_deal_refs.export_support import (
    ordered_deals as _ordered_deals,
)
from intelligence.crm_deal_refs.export_support import (
    ordered_identities as _ordered_identities,
)
from intelligence.crm_deal_refs.export_support import (
    pages as _pages,
)
from intelligence.crm_deal_refs.export_support import (
    snapshot as _snapshot,
)
from intelligence.crm_deal_refs.export_support import (
    value as _value,
)
from intelligence.crm_deal_refs.mapping import map_deal_reference, map_identity_revision
from intelligence.crm_deal_refs.models import (
    IDENTITY_POLICY_VERSION,
    MAX_RAW_PAYLOAD_CHARS,
    MAX_SNAPSHOT_MANIFEST_BYTES,
    SCHEMA_VERSION,
    SOURCE_SYSTEM,
    Boundary,
    Checkpoint,
    DealReference,
    IdentityRevision,
    PageKind,
    PageManifest,
    canonical_digest,
    json_value,
    utc_now,
)
from intelligence.crm_deal_refs.row_validation import bind_full as _bind_full
from intelligence.crm_deal_refs.row_validation import validate_row as _validate_row
from intelligence.crm_deal_refs.snapshot_validation import (
    cursor_of,
    validate_boundary,
    verify_complete,
    verify_partial,
)
from intelligence.crm_deal_refs.snapshot_validation import (
    read_boundary as _read_boundary,
)
from intelligence.repositories.protocols.crm_deal_refs import CrmDealRefsRepository


def seal_boundary(
    repository: CrmDealRefsRepository,
    *,
    source_instance_id: str,
    as_of: str,
    page_size: int,
    max_records: int,
) -> tuple[Boundary, tuple[DealReference, ...], tuple[IdentityRevision, ...]]:
    repository.validate_source_instance(source_instance_id)
    counter = repository.read_identity_boundary()
    if not counter["baseline_ready"]:
        raise RuntimeError("identity revision baseline is not ready")
    capture = utc_now()
    deals = tuple(
        replace(map_deal_reference(row, source_instance_id, as_of), observation_captured_at=capture)
        for page in repository.iter_deal_reference_pages(
            source_instance_id, as_of, page_size, max_records, MAX_RAW_PAYLOAD_CHARS
        )
        for row in page
    )
    _ordered_deals(deals)
    remaining = max_records - len(deals)
    if remaining < 0:
        raise RuntimeError("selected records exceed max-records")
    identities = tuple(
        sorted(
            (
                _identity_at_capture(map_identity_revision(row, source_instance_id, as_of), capture)
                for page in repository.iter_identity_revision_pages(
                    source_instance_id,
                    tuple(sorted({item.source_entity_id for item in deals})),
                    as_of,
                    counter["current_revision"],
                    page_size,
                    remaining,
                )
                for row in page
            ),
            key=lambda item: item.global_revision,
        )
    )
    _ordered_identities(
        identities,
        counter["current_revision"],
        tuple(sorted({item.source_entity_id for item in deals})),
    )
    boundary = _boundary(
        source_instance_id,
        as_of,
        capture,
        page_size,
        max_records,
        counter["current_revision"],
        deals,
        identities,
    )
    return boundary, deals, identities


def capture_matching_boundary(
    repository: CrmDealRefsRepository, boundary: Boundary
) -> tuple[tuple[DealReference, ...], tuple[IdentityRevision, ...]]:
    validate_boundary(boundary)
    repository.validate_source_instance(boundary.source_instance_id)
    counter = repository.read_identity_boundary()
    if (
        not counter["baseline_ready"]
        or counter["current_revision"] < boundary.identity_revision_ceiling
    ):
        raise RuntimeError("identity boundary is unavailable")
    deals = tuple(
        replace(
            map_deal_reference(row, boundary.source_instance_id, boundary.as_of),
            observation_captured_at=boundary.captured_at,
        )
        for page in repository.iter_deal_reference_pages(
            boundary.source_instance_id,
            boundary.as_of,
            boundary.page_size,
            boundary.max_records,
            MAX_RAW_PAYLOAD_CHARS,
        )
        for row in page
    )
    _ordered_deals(deals)
    identities = tuple(
        sorted(
            (
                _identity_at_capture(
                    map_identity_revision(row, boundary.source_instance_id, boundary.as_of),
                    boundary.captured_at,
                )
                for page in repository.iter_identity_revision_pages(
                    boundary.source_instance_id,
                    tuple(sorted({item.source_entity_id for item in deals})),
                    boundary.as_of,
                    boundary.identity_revision_ceiling,
                    boundary.page_size,
                    boundary.max_records - len(deals),
                )
                for row in page
            ),
            key=lambda item: item.global_revision,
        )
    )
    _ordered_identities(
        identities,
        boundary.identity_revision_ceiling,
        tuple(sorted({item.source_entity_id for item in deals})),
    )
    fresh = _boundary(
        boundary.source_instance_id,
        boundary.as_of,
        boundary.captured_at,
        boundary.page_size,
        boundary.max_records,
        boundary.identity_revision_ceiling,
        deals,
        identities,
    )
    if _fingerprints(fresh) != _fingerprints(boundary):
        raise RuntimeError("sealed CRM deal-reference selection drifted")
    return deals, identities


def export_snapshot(
    staging: Path,
    boundary: Boundary,
    deals: tuple[DealReference, ...],
    identities: tuple[IdentityRevision, ...],
) -> None:
    validate_boundary(boundary)
    _prevalidate(boundary, deals, identities)
    root = staging / "snapshots" / "crm" / "deal-refs"
    digest = canonical_digest(json_value(boundary))
    write_new_json(root / "boundary.json", json_value(boundary))
    checkpoint = Checkpoint(SCHEMA_VERSION, digest, 0, 0, 0, 0, None, None, False)
    replace_json(root / "checkpoint.json", json_value(checkpoint))
    manifests: list[PageManifest] = []
    checkpoint = _write(root, boundary, "deal-references", deals, checkpoint, manifests)
    checkpoint = _write(root, boundary, "identity-revisions", identities, checkpoint, manifests)
    checkpoint = replace(
        checkpoint, completed=True, deal_records=len(deals), identity_records=len(identities)
    )
    replace_json(root / "checkpoint.json", json_value(checkpoint))
    write_new_json(
        root / "snapshot-manifest.json",
        _snapshot(boundary, checkpoint, manifests),
        MAX_SNAPSHOT_MANIFEST_BYTES,
    )


def resume_snapshot(
    staging: Path,
    source: Path,
    boundary: Boundary,
    deals: tuple[DealReference, ...],
    identities: tuple[IdentityRevision, ...],
) -> None:
    prior, checkpoint, manifests = verify_partial(source)
    if prior != boundary:
        raise ValueError("resume boundary conflicts with prior evidence")
    expected = {
        "deal-references": _pages(deals, boundary.page_size),
        "identity-revisions": _pages(identities, boundary.page_size),
    }
    for manifest in manifests:
        rows = [_value(item) for item in expected[manifest.kind][manifest.sequence - 1]]
        if source.joinpath(*manifest.path.split("/")).read_bytes() != "".join(
            canonical_json(row) + "\n" for row in rows
        ).encode("utf-8"):
            raise ValueError("committed prefix conflicts with frozen selection")
    root = staging / "snapshots" / "crm" / "deal-refs"
    from intelligence.crm_deal_refs.checkpoints import regular_inventory

    for relative in regular_inventory(source):
        old = source.joinpath(*relative.split("/"))
        new = root.joinpath(*relative.split("/"))
        copy_regular_file(old, new, sha256_file(old), old.stat().st_size)
    copied = list(manifests)
    checkpoint = _write(
        root,
        boundary,
        "deal-references",
        tuple(
            item for page in expected["deal-references"][checkpoint.deal_pages :] for item in page
        ),
        checkpoint,
        copied,
        checkpoint.deal_pages,
        checkpoint.deal_next_cursor,
    )
    checkpoint = _write(
        root,
        boundary,
        "identity-revisions",
        tuple(
            item
            for page in expected["identity-revisions"][checkpoint.identity_pages :]
            for item in page
        ),
        checkpoint,
        copied,
        checkpoint.identity_pages,
        checkpoint.identity_next_cursor,
    )
    checkpoint = replace(
        checkpoint, completed=True, deal_records=len(deals), identity_records=len(identities)
    )
    replace_json(root / "checkpoint.json", json_value(checkpoint))
    write_new_json(
        root / "snapshot-manifest.json",
        _snapshot(boundary, checkpoint, copied),
        MAX_SNAPSHOT_MANIFEST_BYTES,
    )


def verify_snapshot(root: Path) -> dict[str, object]:
    boundary, checkpoint, _, _, _ = verify_complete(root)
    return {
        "accepted": True,
        "boundary_sha256": canonical_digest(json_value(boundary)),
        "deal_record_count": checkpoint.deal_records,
        "identity_record_count": checkpoint.identity_records,
    }


def verified_snapshot_inventory(root: Path) -> tuple[tuple[str, str, int], ...]:
    verify_complete(root)
    return snapshot_regular_inventory(root)


def snapshot_regular_inventory(root: Path) -> tuple[tuple[str, str, int], ...]:
    """Hash exact regular evidence without parsing its domain content."""
    return snapshot_registered_inventory(root, snapshot_regular_sizes(root))


def snapshot_regular_sizes(root: Path) -> tuple[tuple[str, int], ...]:
    """List regular snapshot files and sizes without reading their contents."""
    from intelligence.crm_deal_refs.checkpoints import regular_inventory

    return tuple(
        (path, root.joinpath(*path.split("/")).stat().st_size) for path in regular_inventory(root)
    )


def snapshot_registered_inventory(
    root: Path, expected: tuple[tuple[str, int], ...]
) -> tuple[tuple[str, str, int], ...]:
    """Hash only a prior exact size-checked regular-file inventory."""
    items: list[tuple[str, str, int]] = []
    for path, byte_count in expected:
        candidate = root.joinpath(*path.split("/"))
        if candidate.stat().st_size != byte_count:
            raise ValueError("snapshot file size changed before hashing")
        items.append((path, sha256_file(candidate), byte_count))
    return tuple(items)


def read_boundary(path: Path) -> Boundary:
    """Compatibility import for command handlers."""
    return _read_boundary(path)


def _prevalidate(
    boundary: Boundary, deals: tuple[DealReference, ...], identities: tuple[IdentityRevision, ...]
) -> None:
    deal_rows = [_value(item) for item in deals]
    identity_rows = [_value(item) for item in identities]
    for row in deal_rows:
        _validate_row(row, "deal-references", boundary)
    for row in identity_rows:
        _validate_row(row, "identity-revisions", boundary)
    _bind_full(boundary, deal_rows, identity_rows)


def _identity_at_capture(item: IdentityRevision, captured_at: str) -> IdentityRevision:
    return replace(
        item,
        person_observation_captured_at=(
            captured_at if item.person_status_observed is not None else None
        ),
    )


def _write[T](
    root: Path,
    boundary: Boundary,
    kind: PageKind,
    items: tuple[T, ...],
    checkpoint: Checkpoint,
    manifests: list[PageManifest],
    base: int = 0,
    cursor: str | None = None,
) -> Checkpoint:
    digest = canonical_digest(json_value(boundary))
    for sequence, page in enumerate(_pages(items, boundary.page_size), start=base + 1):
        rows = [_value(item) for item in page]
        path = root / "pages" / kind / f"page-{sequence:06d}.ndjson"
        sha, size = write_new_page(path, rows)
        next_cursor = cursor_of(rows[-1])
        manifest = PageManifest(
            sequence,
            kind,
            path.relative_to(root).as_posix(),
            SOURCE_SYSTEM,
            boundary.source_instance_id,
            IDENTITY_POLICY_VERSION,
            boundary.as_of,
            digest,
            cursor,
            next_cursor,
            cursor_of(rows[0]),
            next_cursor,
            len(rows),
            size,
            sha,
        )
        write_new_json(
            root / "manifests" / kind / f"page-{sequence:06d}.json", json_value(manifest)
        )
        manifests.append(manifest)
        if kind == "deal-references":
            checkpoint = replace(
                checkpoint,
                deal_pages=sequence,
                deal_records=checkpoint.deal_records + len(rows),
                deal_next_cursor=next_cursor,
            )
        else:
            checkpoint = replace(
                checkpoint,
                identity_pages=sequence,
                identity_records=checkpoint.identity_records + len(rows),
                identity_next_cursor=next_cursor,
            )
        replace_json(root / "checkpoint.json", json_value(checkpoint))
        cursor = next_cursor
    return checkpoint
