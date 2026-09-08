"""Strict persisted boundary, page, checkpoint, and artifact validation."""

from __future__ import annotations

from pathlib import Path

from intelligence.artifacts import canonical_json, sha256_file
from intelligence.crm_deal_refs.checkpoints import read_json, regular_inventory
from intelligence.crm_deal_refs.export_support import validate_checkpoint_limits
from intelligence.crm_deal_refs.models import (
    IDENTITY_POLICY_VERSION,
    MAX_PAGE_BYTES,
    MAX_PAGE_SIZE,
    MAX_RECORDS,
    MAX_SNAPSHOT_MANIFEST_BYTES,
    QUERY_VERSION,
    SCHEMA_VERSION,
    SOURCE_SYSTEM,
    Boundary,
    Checkpoint,
    PageKind,
    PageManifest,
    canonical_digest,
    json_value,
    parse_cutoff,
    safe_identifier,
)
from intelligence.crm_deal_refs.row_validation import bind_full as _verify_full_binding
from intelligence.crm_deal_refs.row_validation import deal_key as _deal_key
from intelligence.crm_deal_refs.snapshot_pages import page_rows


def read_boundary(path: Path) -> Boundary:
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != set(Boundary.__dataclass_fields__):
        raise ValueError("snapshot boundary schema is invalid")
    terminal = value["source_terminal_key"]
    key = None if terminal is None else _deal_key(terminal)
    boundary = Boundary(
        _positive(value["schema_version"], "schema_version"),
        _text(value["query_version"], "query_version"),
        _text(value["source_system"], "source_system"),
        _text(value["source_instance_id"], "source_instance_id"),
        _text(value["identity_policy_version"], "identity_policy_version"),
        _timestamp(value["as_of"], "as_of"),
        _timestamp(value["captured_at"], "captured_at"),
        _positive(value["page_size"], "page_size"),
        _positive(value["max_records"], "max_records"),
        _nonnegative(value["identity_revision_ceiling"], "ceiling"),
        _nonnegative(value["source_membership_count"], "count"),
        key,
        _digest(value["source_membership_sha256"]),
        _digest(value["immutable_facts_sha256"]),
        _digest(value["identity_facts_sha256"]),
        _digest(value["mutable_observations_sha256"]),
    )
    validate_boundary(boundary)
    return boundary


def validate_boundary(boundary: Boundary) -> None:
    if (
        boundary.schema_version,
        boundary.query_version,
        boundary.source_system,
        boundary.identity_policy_version,
    ) != (SCHEMA_VERSION, QUERY_VERSION, SOURCE_SYSTEM, IDENTITY_POLICY_VERSION):
        raise ValueError("snapshot schema or policy is incompatible")
    safe_identifier(boundary.source_instance_id, "source instance")
    if parse_cutoff(boundary.as_of) != boundary.as_of:
        raise ValueError("snapshot boundary cutoff is invalid")
    if parse_cutoff(boundary.captured_at) != boundary.captured_at:
        raise ValueError("snapshot boundary capture is invalid")
    if (
        boundary.page_size < 1
        or boundary.page_size > MAX_PAGE_SIZE
        or boundary.max_records < 1
        or boundary.max_records > MAX_RECORDS
        or boundary.identity_revision_ceiling < 0
        or boundary.source_membership_count < 0
    ):
        raise ValueError("snapshot boundary limits are invalid")
    if (boundary.source_membership_count == 0) != (boundary.source_terminal_key is None):
        raise ValueError("snapshot boundary terminal key is invalid")
    if boundary.source_terminal_key is not None:
        _deal_key(json_value(boundary.source_terminal_key))


def read_checkpoint(path: Path, boundary_sha: str) -> Checkpoint:
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != set(Checkpoint.__dataclass_fields__):
        raise ValueError("snapshot checkpoint schema is invalid")
    completed = value["completed"]
    if not isinstance(completed, bool):
        raise ValueError("snapshot checkpoint completion is invalid")
    checkpoint = Checkpoint(
        _positive(value["schema_version"], "schema_version"),
        _digest(value["boundary_sha256"]),
        _nonnegative(value["deal_pages"], "deal_pages"),
        _nonnegative(value["identity_pages"], "identity_pages"),
        _nonnegative(value["deal_records"], "deal_records"),
        _nonnegative(value["identity_records"], "identity_records"),
        _optional_text(value["deal_next_cursor"]),
        _optional_text(value["identity_next_cursor"]),
        completed,
    )
    if checkpoint.schema_version != SCHEMA_VERSION or checkpoint.boundary_sha256 != boundary_sha:
        raise ValueError("snapshot checkpoint boundary is invalid")
    return checkpoint


def verify_complete(
    root: Path,
) -> tuple[
    Boundary, Checkpoint, tuple[PageManifest, ...], list[dict[str, object]], list[dict[str, object]]
]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot root is unsafe")
    boundary = read_boundary(root / "boundary.json")
    digest = canonical_digest(json_value(boundary))
    checkpoint = read_checkpoint(root / "checkpoint.json", digest)
    validate_checkpoint_limits(boundary, checkpoint)
    if not checkpoint.completed:
        raise ValueError("snapshot is partial")
    manifests = _snapshot_manifests(
        read_json(root / "snapshot-manifest.json", MAX_SNAPSHOT_MANIFEST_BYTES),
        boundary,
        digest,
        checkpoint,
    )
    deals, identities = verify_pages(root, boundary, checkpoint, manifests)
    _verify_full_binding(boundary, deals, identities)
    expected = {"boundary.json", "checkpoint.json", "snapshot-manifest.json"}
    for item in manifests:
        expected.update((item.path, f"manifests/{item.kind}/page-{item.sequence:06d}.json"))
    if set(regular_inventory(root)) != expected:
        raise ValueError("snapshot inventory is not exact")
    return boundary, checkpoint, manifests, deals, identities


def verify_partial(root: Path) -> tuple[Boundary, Checkpoint, tuple[PageManifest, ...]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("snapshot root is unsafe")
    boundary = read_boundary(root / "boundary.json")
    checkpoint = read_checkpoint(root / "checkpoint.json", canonical_digest(json_value(boundary)))
    validate_checkpoint_limits(boundary, checkpoint)
    if checkpoint.completed or (root / "snapshot-manifest.json").exists():
        raise ValueError("partial snapshot terminal evidence is invalid")
    manifests = _prefix_manifests(root, checkpoint)
    verify_pages(root, boundary, checkpoint, manifests)
    expected = {"boundary.json", "checkpoint.json"}
    for item in manifests:
        expected.update((item.path, f"manifests/{item.kind}/page-{item.sequence:06d}.json"))
    if set(regular_inventory(root)) != expected:
        raise ValueError("partial inventory is not exact")
    return boundary, checkpoint, manifests


def verify_pages(
    root: Path, boundary: Boundary, checkpoint: Checkpoint, manifests: tuple[PageManifest, ...]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    groups: dict[str, list[PageManifest]] = {"deal-references": [], "identity-revisions": []}
    for manifest in manifests:
        groups[manifest.kind].append(manifest)
    deals = _verify_group(
        root, boundary, tuple(groups["deal-references"]), checkpoint.deal_pages, "deal-references"
    )
    identities = _verify_group(
        root,
        boundary,
        tuple(groups["identity-revisions"]),
        checkpoint.identity_pages,
        "identity-revisions",
    )
    if len(deals) != checkpoint.deal_records or len(identities) != checkpoint.identity_records:
        raise ValueError("checkpoint record counts are invalid")
    if checkpoint.deal_next_cursor != (
        None if not groups["deal-references"] else groups["deal-references"][-1].next_cursor
    ):
        raise ValueError("checkpoint deal cursor is invalid")
    if checkpoint.identity_next_cursor != (
        None if not groups["identity-revisions"] else groups["identity-revisions"][-1].next_cursor
    ):
        raise ValueError("checkpoint identity cursor is invalid")
    return deals, identities


def _verify_group(
    root: Path, boundary: Boundary, items: tuple[PageManifest, ...], count: int, kind: PageKind
) -> list[dict[str, object]]:
    if len(items) != count:
        raise ValueError("page group count is invalid")
    rows: list[dict[str, object]] = []
    cursor: str | None = None
    for sequence, manifest in enumerate(items, start=1):
        _validate_manifest(manifest, boundary, kind, sequence, cursor)
        data = root.joinpath(*manifest.path.split("/"))
        if sha256_file(data) != manifest.sha256 or data.stat().st_size != manifest.byte_count:
            raise ValueError("page checksum is invalid")
        sidecar = root / "manifests" / kind / f"page-{sequence:06d}.json"
        if read_json(sidecar) != json_value(manifest):
            raise ValueError("page sidecar is invalid")
        page = page_rows(data, kind, boundary)
        if len(page) != manifest.count or not page:
            raise ValueError("page count is invalid")
        if (manifest.first_key, manifest.last_key, manifest.next_cursor) != (
            cursor_of(page[0]),
            cursor_of(page[-1]),
            cursor_of(page[-1]),
        ):
            raise ValueError("page cursor values are invalid")
        rows.extend(page)
        cursor = manifest.next_cursor
    return rows


def cursor_of(row: dict[str, object]) -> str:
    key = row.get("key")
    if isinstance(key, dict):
        return canonical_json(key)
    revision = row.get("global_revision")
    if isinstance(revision, int) and not isinstance(revision, bool):
        return f"revision:{revision:020d}"
    raise ValueError("row cursor is invalid")


def _snapshot_manifests(
    value: object, boundary: Boundary, digest: str, checkpoint: Checkpoint
) -> tuple[PageManifest, ...]:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "boundary_sha256",
        "source_system",
        "source_instance_id",
        "identity_policy_version",
        "as_of",
        "deal_page_count",
        "identity_page_count",
        "deal_record_count",
        "identity_record_count",
        "page_manifests_sha256",
        "page_manifests",
    }:
        raise ValueError("snapshot manifest is invalid")
    raw = value.get("page_manifests")
    if (
        value.get("schema_version") != SCHEMA_VERSION
        or value.get("boundary_sha256") != digest
        or value.get("source_system") != SOURCE_SYSTEM
        or value.get("source_instance_id") != boundary.source_instance_id
        or value.get("identity_policy_version") != IDENTITY_POLICY_VERSION
        or value.get("as_of") != boundary.as_of
        or not isinstance(raw, list)
    ):
        raise ValueError("snapshot manifest digest is invalid")
    counts = (
        value.get("deal_page_count"),
        value.get("identity_page_count"),
        value.get("deal_record_count"),
        value.get("identity_record_count"),
    )
    if any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in counts):
        raise ValueError("snapshot manifest counts are invalid")
    if counts != (
        checkpoint.deal_pages,
        checkpoint.identity_pages,
        checkpoint.deal_records,
        checkpoint.identity_records,
    ):
        raise ValueError("snapshot manifest counts are invalid")
    if len(raw) != checkpoint.deal_pages + checkpoint.identity_pages:
        raise ValueError("snapshot manifest page count is invalid")
    if canonical_digest(raw) != value.get("page_manifests_sha256"):
        raise ValueError("snapshot manifest digest is invalid")
    items = tuple(_manifest(item) for item in raw)
    if items != tuple(sorted(items, key=lambda item: (item.kind, item.sequence))):
        raise ValueError("snapshot manifest ordering is invalid")
    return items


def _prefix_manifests(root: Path, checkpoint: Checkpoint) -> tuple[PageManifest, ...]:
    items: list[PageManifest] = []
    for kind, count in (
        ("deal-references", checkpoint.deal_pages),
        ("identity-revisions", checkpoint.identity_pages),
    ):
        for sequence in range(1, count + 1):
            items.append(
                _manifest(read_json(root / "manifests" / kind / f"page-{sequence:06d}.json"))
            )
    return tuple(items)


def _manifest(value: object) -> PageManifest:
    if not isinstance(value, dict) or set(value) != set(PageManifest.__dataclass_fields__):
        raise ValueError("page manifest schema is invalid")
    kind = value.get("kind")
    sequence = _positive(value.get("sequence"), "sequence")
    if kind not in {"deal-references", "identity-revisions"}:
        raise ValueError("page kind is invalid")
    path = _text(value.get("path"), "path")
    if path != f"pages/{kind}/page-{sequence:06d}.ndjson":
        raise ValueError("page path is invalid")
    byte_count = _nonnegative(value.get("byte_count"), "bytes")
    if byte_count > MAX_PAGE_BYTES:
        raise ValueError("page byte count exceeds snapshot limit")
    return PageManifest(
        sequence,
        kind,
        path,
        _text(value.get("source_system"), "source system"),
        _text(value.get("source_instance_id"), "source instance"),
        _text(value.get("identity_policy_version"), "policy"),
        _timestamp(value.get("as_of"), "as_of"),
        _digest(value.get("boundary_sha256")),
        _optional_text(value.get("current_cursor")),
        _optional_text(value.get("next_cursor")),
        _optional_text(value.get("first_key")),
        _optional_text(value.get("last_key")),
        _positive(value.get("count"), "count"),
        byte_count,
        _digest(value.get("sha256")),
    )


def _validate_manifest(
    manifest: PageManifest, boundary: Boundary, kind: PageKind, sequence: int, cursor: str | None
) -> None:
    if (
        manifest.kind,
        manifest.sequence,
        manifest.current_cursor,
        manifest.source_system,
        manifest.source_instance_id,
        manifest.identity_policy_version,
        manifest.as_of,
        manifest.boundary_sha256,
    ) != (
        kind,
        sequence,
        cursor,
        SOURCE_SYSTEM,
        boundary.source_instance_id,
        IDENTITY_POLICY_VERSION,
        boundary.as_of,
        canonical_digest(json_value(boundary)),
    ):
        raise ValueError("page manifest boundary is invalid")


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or parse_cutoff(value) != value:
        raise ValueError(f"{field} is invalid")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} is invalid")
    return value


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value, "value")


def _positive(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{field} is invalid")
    return value


def _nonnegative(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} is invalid")
    return value


def _digest(value: object) -> str:
    text = _text(value, "digest")
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("digest is invalid")
    return text
