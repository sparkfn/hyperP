"""Typed parsing and bounded activity evidence decoding for dataset admission."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from intelligence.crm.activities.model_parsing import record_from_mapping
from intelligence.crm.activities.models import ArchiveRecord
from intelligence.crm_deal_refs.models import (
    Availability,
    Boundary,
    DealKey,
    DealReference,
    IdentityRevision,
    IdentityStatus,
)
from intelligence.datasets.bounds import (
    MAX_INPUT_BYTES,
    MAX_INPUT_ENTRIES,
    MAX_INPUT_FILE_BYTES,
    ReadBudget,
    canonical_json_object,
    digest_file,
)
from intelligence.datasets.models import MAX_ACTIVITY_RECORDS
from intelligence.models import OutputInventory

type RecordPages = Callable[[Path, ReadBudget], tuple[Path, ...]]


@dataclass(frozen=True)
class ActivityRows:
    """The archive records partitioned by their accepted source disposition."""

    records: tuple[ArchiveRecord, ...]
    accepted_ids: frozenset[str]
    rejected_ids: frozenset[str]
    quarantined_ids: frozenset[str]


def deal_reference(value: Mapping[str, object]) -> DealReference:
    """Decode one strict #353 deal-reference row."""
    key = _mapping(value.get("key"), "deal key")
    version = _integer(key, "source_record_version")
    return DealReference(
        _text(value, "source_system"),
        _text(value, "source_instance_id"),
        _text(value, "identity_policy_version"),
        DealKey(_text(key, "source_record_id"), version, _text(key, "source_record_pk")),
        _text(value, "record_hash"),
        _text(value, "source_entity_type"),
        _text(value, "source_entity_id"),
        _optional_text(value, "entity_key"),
        _optional_text(value, "category_id"),
        _optional_text(value, "stage_id"),
        _optional_text(value, "stage_semantic_id"),
        _optional_text(value, "source_outcome_ref"),
        _optional_text(value, "observed_at"),
        _optional_text(value, "available_at"),
        _optional_text(value, "first_known_at"),
        _optional_text(value, "source_event_at"),
        _optional_text(value, "source_effective_at"),
        _optional_text(value, "source_close_date"),
        _availability(value),
        _boolean(value, "point_in_time_eligible"),
        _optional_text(value, "lifecycle_status_observed"),
        _optional_text(value, "link_status_observed"),
        _text(value, "observation_captured_at"),
    )


def identity_revision(value: Mapping[str, object]) -> IdentityRevision:
    """Decode one strict #353 identity-revision row."""
    global_revision = _integer(value, "global_revision")
    resolution_revision = _integer(value, "resolution_revision")
    return IdentityRevision(
        _text(value, "event_id"),
        global_revision,
        _text(value, "source_system"),
        _text(value, "source_instance_id"),
        _text(value, "identity_policy_version"),
        _text(value, "source_entity_id"),
        _identity_status(value.get("link_status")),
        _optional_text(value, "hyperp_person_id"),
        _optional_text(value, "person_status_observed"),
        _optional_text(value, "person_observation_captured_at"),
        _text(value, "resolution_kind"),
        resolution_revision,
        _optional_text(value, "effective_at"),
        _optional_text(value, "available_at"),
        _optional_text(value, "first_known_at"),
        _availability(value),
        _boolean(value, "person_reference_eligible"),
    )


def activity_rows(snapshot: Path, record_pages: RecordPages) -> ActivityRows:
    """Read bounded canonical records and preserve their source disposition partition."""
    budget = ReadBudget(MAX_INPUT_BYTES, MAX_INPUT_ENTRIES, MAX_ACTIVITY_RECORDS)
    accepted = _accepted_records(snapshot, record_pages, budget)
    rejected = _disposition_records(snapshot, "rejected.json", budget)
    quarantined = _disposition_records(snapshot, "quarantined.json", budget)
    records = tuple(
        sorted((*accepted, *rejected, *quarantined), key=lambda item: item.source_record_pk)
    )
    if len({item.source_record_pk for item in records}) != len(records):
        raise ValueError("activity disposition identities overlap")
    return ActivityRows(
        records,
        frozenset(item.source_record_pk for item in accepted),
        frozenset(item.source_record_pk for item in rejected),
        frozenset(item.source_record_pk for item in quarantined),
    )


def verified_selected_count(
    snapshot: Path,
    inventory: tuple[OutputInventory, ...],
    prefix: str,
) -> int:
    """Verify State-pinned manifest bytes before its count can influence a read budget."""
    expected = _manifest_inventory_item(inventory, prefix)
    budget = ReadBudget(MAX_INPUT_FILE_BYTES * 2, 2, 1)
    path = snapshot / "manifest.json"
    actual = digest_file(path, budget, maximum_file_bytes=MAX_INPUT_FILE_BYTES)
    if actual != expected.sha256:
        raise ValueError("activity manifest checksum differs from accepted inventory")
    count = json_document(path, budget).get("selected_count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise ValueError("activity selected count is invalid")
    if count < 0 or count > MAX_ACTIVITY_RECORDS:
        raise ValueError("activity selected count is invalid")
    return count


def json_document(path: Path, budget: ReadBudget) -> dict[str, object]:
    """Read one bounded canonical input object."""
    return canonical_json_object(path, budget, maximum_file_bytes=MAX_INPUT_FILE_BYTES)


def inventory_dict(item: OutputInventory) -> dict[str, object]:
    """Return canonical inventory serialization used by dataset pin digests."""
    return {
        "byte_count": item.byte_count,
        "relative_path": item.relative_path,
        "sha256": item.sha256,
    }


def boundary_dict(boundary: Boundary) -> dict[str, object]:
    """Return canonical #353 boundary serialization including its nested terminal key."""
    value = {field: getattr(boundary, field) for field in Boundary.__dataclass_fields__}
    terminal = boundary.source_terminal_key
    value["source_terminal_key"] = (
        None
        if terminal is None
        else {
            "source_record_id": terminal.source_record_id,
            "source_record_pk": terminal.source_record_pk,
            "source_record_version": terminal.source_record_version,
        }
    )
    return value


def _accepted_records(
    snapshot: Path, record_pages: RecordPages, budget: ReadBudget
) -> list[ArchiveRecord]:
    records: list[ArchiveRecord] = []
    for page in record_pages(snapshot / "records", budget):
        values = _records(json_document(page, budget), "accepted activity records", budget)
        records.extend(_record_values(values))
    return records


def _disposition_records(snapshot: Path, filename: str, budget: ReadBudget) -> list[ArchiveRecord]:
    values = _records(
        json_document(snapshot / filename, budget), "activity disposition evidence", budget
    )
    records: list[ArchiveRecord] = []
    for row in values:
        mapping = _mapping(row, "activity disposition row")
        records.append(record_from_mapping(_mapping(mapping.get("record"), "activity record")))
    return records


def _records(value: Mapping[str, object], label: str, budget: ReadBudget) -> list[object]:
    records = value.get("records")
    if not isinstance(records, list):
        raise ValueError(f"{label} is invalid")
    budget.rows(len(records))
    return records


def _record_values(values: Sequence[object]) -> list[ArchiveRecord]:
    return [record_from_mapping(_mapping(value, "accepted activity record")) for value in values]


def _manifest_inventory_item(
    inventory: tuple[OutputInventory, ...], prefix: str
) -> OutputInventory:
    matches = tuple(item for item in inventory if item.relative_path == f"{prefix}manifest.json")
    if len(matches) != 1 or matches[0].byte_count > MAX_INPUT_FILE_BYTES:
        raise ValueError("activity manifest is absent or exceeds accepted inventory bounds")
    return matches[0]


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} is invalid")
    return value


def _integer(value: Mapping[str, object], key: str) -> int:
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise ValueError(f"{key} is invalid")
    return result


def _availability(value: Mapping[str, object]) -> Availability:
    result = value.get("availability")
    if result == "known_by_cutoff":
        return "known_by_cutoff"
    if result == "unknown":
        return "unknown"
    raise ValueError("availability is invalid")


def _identity_status(value: object) -> IdentityStatus:
    statuses = {"resolved", "unresolved", "pending_review", "blocked", "rejected", "retired"}
    if value not in statuses:
        raise ValueError("identity status is invalid")
    return cast(IdentityStatus, value)


def _boolean(value: Mapping[str, object], key: str) -> bool:
    result = value.get(key)
    if not isinstance(result, bool):
        raise ValueError(f"{key} is invalid")
    return result


def _text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result


def _optional_text(value: Mapping[str, object], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} is invalid")
    return result
