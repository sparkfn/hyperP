"""Strict value types and canonical encodings for reviewed Intelligence datasets."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from intelligence.artifacts import canonical_json
from intelligence.crm.activities.models import ArchiveRecord
from intelligence.crm_deal_refs.models import Boundary, DealReference, IdentityRevision

DATASET_DEFINITION = "crm-deal-state-v1"
DATASET_SCHEMA_VERSION = "crm-deal-state-dataset-v1"
DATASET_DESCRIPTOR_SCHEMA = "crm-deal-state-acceptance-descriptor-v1"
DATASET_VERIFICATION_SCHEMA = "crm-deal-state-verification-v1"
CODE_VERSION = "intelligence-datasets-v1"
SOURCE_SYSTEM = "bitrix_chat"
ACTIVITY_PROVENANCE = {
    "source_population": "neo4j_existing_records",
    "completeness": "legacy_partial_snapshot",
    "bitrix_completeness_asserted": False,
}
MAX_CATALOG_RUNS = 1_000
MAX_DATASET_ROWS = 10_000
MAX_ACTIVITY_RECORDS = 100_000

RowDisposition = Literal["labeled", "censored", "ineligible", "unresolved"]


def canonical_digest(value: object) -> str:
    """Return a SHA-256 digest of one canonical JSON value."""
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def code_contract_fingerprint() -> str:
    """Hash the complete reviewed datasets source set independent of checkout newlines."""
    root = Path(__file__).parent
    digest = sha256()
    for path in sorted(root.glob("*.py"), key=lambda item: item.name):
        text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(text.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def parse_utc(value: str, field: str) -> datetime:
    """Parse a canonical timezone-aware UTC instant without accepting local ambiguity."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    normalized = parsed.astimezone(UTC)
    if normalized.isoformat().replace("+00:00", "Z") != value:
        raise ValueError(f"{field} must be canonical UTC")
    return normalized


def duration_seconds(start: str, end: str) -> int:
    """Return a non-negative integer duration without fractional-time encoding."""
    seconds = int((parse_utc(end, "end") - parse_utc(start, "start")).total_seconds())
    if seconds < 0:
        raise ValueError("duration cannot be negative")
    return seconds


def safe_component(value: str, field: str) -> str:
    """Accept the repository's conservative run/checkpoint identifier grammar."""
    if (
        not value
        or len(value) > 200
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


@dataclass(frozen=True)
class DatasetRequest:
    """The complete fixed request whose digest controls deterministic replay."""

    deal_refs_run_id: str
    activities_checkpoint_id: str
    activities_accepted_run_id: str
    definition: str
    feature_cutoff: str
    label_cutoff: str
    seed: int

    def __post_init__(self) -> None:
        safe_component(self.deal_refs_run_id, "deal reference run")
        safe_component(self.activities_checkpoint_id, "activity checkpoint")
        safe_component(self.activities_accepted_run_id, "activity accepted run")
        if self.definition != DATASET_DEFINITION:
            raise ValueError("dataset definition is unsupported")
        feature = parse_utc(self.feature_cutoff, "feature cutoff")
        label = parse_utc(self.label_cutoff, "label cutoff")
        if feature >= label:
            raise ValueError("feature cutoff must precede label cutoff")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ValueError("seed must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "activities_accepted_run_id": self.activities_accepted_run_id,
            "activities_checkpoint_id": self.activities_checkpoint_id,
            "deal_refs_run_id": self.deal_refs_run_id,
            "definition": self.definition,
            "feature_cutoff": self.feature_cutoff,
            "label_cutoff": self.label_cutoff,
            "seed": self.seed,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.as_dict())


@dataclass(frozen=True)
class DealInput:
    """State-admitted deal-reference snapshot converted to typed dataset input."""

    run_id: str
    boundary: Boundary
    boundary_digest: str
    inventory_digest: str
    snapshot_manifest_digest: str
    deals: tuple[DealReference, ...]
    identities: tuple[IdentityRevision, ...]

    def pin(self) -> dict[str, object]:
        return {
            "boundary_digest": self.boundary_digest,
            "inventory_digest": self.inventory_digest,
            "run_id": self.run_id,
            "snapshot_manifest_digest": self.snapshot_manifest_digest,
        }


@dataclass(frozen=True)
class ActivityInput:
    """State-admitted partial archive snapshot converted to typed dataset input."""

    checkpoint_id: str
    accepted_run_id: str
    logical_snapshot_id: str
    descriptor_digest: str
    boundary_digest: str
    manifest_digest: str
    inventory_digest: str
    source_instance_id: str
    source_key: str
    records: tuple[ArchiveRecord, ...]
    accepted_ids: frozenset[str]
    rejected_ids: frozenset[str]
    quarantined_ids: frozenset[str]

    def pin(self) -> dict[str, object]:
        return {
            "accepted_run_id": self.accepted_run_id,
            "boundary_digest": self.boundary_digest,
            "checkpoint_id": self.checkpoint_id,
            "descriptor_digest": self.descriptor_digest,
            "inventory_digest": self.inventory_digest,
            "logical_snapshot_id": self.logical_snapshot_id,
            "manifest_digest": self.manifest_digest,
        }


@dataclass(frozen=True)
class AcceptedInputs:
    """All immutable input evidence needed by a supervised build or verification child."""

    request: DatasetRequest
    deals: DealInput
    activities: ActivityInput

    def config(self) -> dict[str, object]:
        return {
            "code_contract_fingerprint": code_contract_fingerprint(),
            "code_version": CODE_VERSION,
            "definition": self.request.definition,
            "feature_cutoff": self.request.feature_cutoff,
            "inputs": {"activities": self.activities.pin(), "deal_refs": self.deals.pin()},
            "label_cutoff": self.request.label_cutoff,
            "seed": self.request.seed,
        }


def parse_config(value: object) -> dict[str, object]:
    """Validate the exact reviewed persisted dataset input/configuration contract."""
    if not isinstance(value, dict):
        raise ValueError("dataset configuration is invalid")
    expected = {
        "code_contract_fingerprint",
        "code_version",
        "definition",
        "feature_cutoff",
        "inputs",
        "label_cutoff",
        "seed",
    }
    if set(value) != expected:
        raise ValueError("dataset configuration schema is invalid")
    if value.get("definition") != DATASET_DEFINITION:
        raise ValueError("dataset configuration definition is incompatible")
    _config_text(value, "code_version")
    _digest_text(value.get("code_contract_fingerprint"), "dataset configuration fingerprint")
    feature = _config_text(value, "feature_cutoff")
    label = _config_text(value, "label_cutoff")
    seed = value.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("dataset configuration seed is invalid")
    inputs = value.get("inputs")
    if not isinstance(inputs, dict) or set(inputs) != {"activities", "deal_refs"}:
        raise ValueError("dataset configuration input pins are invalid")
    deals = _config_mapping(inputs, "deal_refs")
    activities = _config_mapping(inputs, "activities")
    _exact_pin(deals, {"boundary_digest", "inventory_digest", "run_id", "snapshot_manifest_digest"})
    _exact_pin(
        activities,
        {
            "accepted_run_id",
            "boundary_digest",
            "checkpoint_id",
            "descriptor_digest",
            "inventory_digest",
            "logical_snapshot_id",
            "manifest_digest",
        },
    )
    DatasetRequest(
        _config_text(deals, "run_id"),
        _config_text(activities, "checkpoint_id"),
        _config_text(activities, "accepted_run_id"),
        DATASET_DEFINITION,
        feature,
        label,
        seed,
    )
    return {
        "code_contract_fingerprint": _config_text(value, "code_contract_fingerprint"),
        "code_version": _config_text(value, "code_version"),
        "definition": DATASET_DEFINITION,
        "feature_cutoff": feature,
        "inputs": {"activities": dict(activities), "deal_refs": dict(deals)},
        "label_cutoff": label,
        "seed": seed,
    }


def current_config_compatible(value: object) -> dict[str, object]:
    """Require a persisted config to match this executable before reconstruction."""
    parsed = parse_config(value)
    if (
        parsed["code_version"] != CODE_VERSION
        or parsed["code_contract_fingerprint"] != code_contract_fingerprint()
    ):
        raise ValueError("dataset configuration is incompatible with current executable")
    return parsed


def _config_mapping(value: dict[str, object], key: str) -> dict[str, object]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise ValueError("dataset configuration input pin is invalid")
    return result


def _exact_pin(value: dict[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValueError("dataset configuration input pin schema is invalid")
    for key, item in value.items():
        if key.endswith("digest"):
            if (
                not isinstance(item, str)
                or len(item) != 64
                or any(character not in "0123456789abcdef" for character in item)
            ):
                raise ValueError("dataset configuration digest pin is invalid")
        else:
            _config_text(value, key)


def _config_text(value: dict[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"dataset configuration {key} is invalid")
    return result


def _digest_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


@dataclass(frozen=True)
class DatasetRow:
    """One logical deal row with explicit censored/missing partial-history fields."""

    source_system: str
    source_instance_id: str
    source_entity_id: str
    feature_source_record_pk: str | None
    horizon_source_record_pk: str | None
    person_id: str | None
    category_id: str | None
    stage_id: str | None
    stage_semantic_id: str | None
    deal_age_seconds: int | None
    source_version_age_seconds: int | None
    archived_activity_count_lower_bound: int | None
    companion_call_count_lower_bound: int | None
    seconds_since_last_eligible_archived_activity: int | None
    activity_coverage: str
    activity_missingness_reason: str | None
    identity_reason: str | None
    feature_reason: str | None
    label: str | None
    label_reason: str | None
    disposition: RowDisposition

    def as_dict(self) -> dict[str, object]:
        return {
            "activity_coverage": self.activity_coverage,
            "activity_missingness_reason": self.activity_missingness_reason,
            "archived_activity_count_lower_bound": self.archived_activity_count_lower_bound,
            "category_id": self.category_id,
            "companion_call_count_lower_bound": self.companion_call_count_lower_bound,
            "deal_age_seconds": self.deal_age_seconds,
            "disposition": self.disposition,
            "feature_reason": self.feature_reason,
            "feature_source_record_pk": self.feature_source_record_pk,
            "horizon_source_record_pk": self.horizon_source_record_pk,
            "identity_reason": self.identity_reason,
            "label": self.label,
            "label_reason": self.label_reason,
            "person_id": self.person_id,
            "seconds_since_last_eligible_archived_activity": (
                self.seconds_since_last_eligible_archived_activity
            ),
            "source_entity_id": self.source_entity_id,
            "source_instance_id": self.source_instance_id,
            "source_system": self.source_system,
            "source_version_age_seconds": self.source_version_age_seconds,
            "stage_id": self.stage_id,
            "stage_semantic_id": self.stage_semantic_id,
        }
