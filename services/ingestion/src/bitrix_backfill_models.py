"""Typed contracts for corrective Bitrix generation topology and coverage."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal

from src.bitrix_ingestion_models import BitrixStreamKey
from src.models import JsonValue
from src.resumable import CheckpointDescriptor

GenerationStatus = Literal[
    "allocated",
    "backfilling",
    "reconciling",
    "frozen",
    "qualified",
    "accepted",
    "failed",
    "rejected",
    "superseded",
]
CoverageDisposition = Literal[
    "created",
    "existing_same_hash",
    "updated_projection",
    "scope_unchanged",
    "excluded_out_of_scope",
    "quarantined_owner_unresolved",
    "conflict",
    "failed",
]


@dataclass(frozen=True)
class GenerationProvenance:
    """Immutable provenance required before a corrective generation is allocated."""

    repository_sha: str
    image_digest: str
    configuration_digest: str
    source_contract_uuid: str
    boundary_digest: str

    def __post_init__(self) -> None:
        for name, value in (
            ("repository_sha", self.repository_sha),
            ("image_digest", self.image_digest),
            ("configuration_digest", self.configuration_digest),
            ("source_contract_uuid", self.source_contract_uuid),
            ("boundary_digest", self.boundary_digest),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class GenerationRunContext:
    """Corrective-generation identity carried by one child split run."""

    generation_id: str
    boundary_digest: str
    configuration_digest: str

    def __post_init__(self) -> None:
        if not all(value.strip() for value in vars(self).values()):
            raise ValueError("generation run identity values must be non-empty")


@dataclass(frozen=True)
class KnownOwnerMembershipSet:
    generation_id: str
    membership_set_id: str
    digest: str
    deal_ids: tuple[str, ...]


@dataclass(frozen=True)
class CoverageEntry:
    """One terminal source identity accounting row."""

    source_identity: str
    source_boundary: str
    disposition: CoverageDisposition
    source_observation_hash: str
    terminal: bool = True
    deal_id: str | None = None
    scope_state: str | None = None
    entity_key: str | None = None
    category_id: str | None = None
    stage_id: str | None = None
    census_epoch: int | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.source_identity.strip() or not self.source_boundary.strip():
            raise ValueError("coverage identity and boundary must be non-empty")
        if not self.source_observation_hash.strip():
            raise ValueError("coverage source observation hash must be non-empty")
        if self.census_epoch is not None and (
            isinstance(self.census_epoch, bool) or self.census_epoch < 1
        ):
            raise ValueError("coverage census_epoch must be positive")

    @property
    def outcome_digest(self) -> str:
        payload = {
            "source_identity": self.source_identity,
            "source_boundary": self.source_boundary,
            "disposition": self.disposition,
            "source_observation_hash": self.source_observation_hash,
            "terminal": self.terminal,
            "deal_id": self.deal_id,
            "scope_state": self.scope_state,
            "entity_key": self.entity_key,
            "category_id": self.category_id,
            "stage_id": self.stage_id,
            "census_epoch": self.census_epoch,
            "detail": self.detail,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(b"bitrix-coverage-outcome-v1\x00" + encoded).hexdigest()


def initial_stream_checkpoint(
    stream_key: BitrixStreamKey,
    *,
    source_window: dict[str, JsonValue],
    census_epoch: int = 1,
) -> CheckpointDescriptor:
    """Build the fixed version-1 checkpoint contract for a child stream."""
    validate_stream_source_window(stream_key, source_window)
    if stream_key == "crm_deals":
        phase = "scoped_deal_census_v1"
        cursor: dict[str, JsonValue] = {
            "last_deal_id": None,
            "census_epoch": census_epoch,
        }
        connector = "bitrix-crm-deals-keyset-v1"
        boundary = "exclusive_last_deal_id"
    elif stream_key == "crm_activities":
        phase = "crm_activity_keyset_v1"
        cursor = {"last_activity_id": None}
        connector = "bitrix-crm-activity-keyset-v1"
        boundary = "exclusive_last_activity_id"
    else:
        phase = "openlines_conversation_replay_v1"
        cursor = {"crm_start": None}
        connector = "bitrix-openlines-replay-v1"
        boundary = "at_least_once_page_start"
    return CheckpointDescriptor(
        phase=phase,
        cursor=cursor,
        source_window=dict(source_window),
        last_committed_record_id=None,
        connector_version=connector,
        schema_version=1,
        replay_boundary=boundary,
    )


def validate_stream_source_window(
    stream_key: BitrixStreamKey,
    source_window: dict[str, JsonValue],
) -> None:
    """Reject incomplete or ambiguous corrective source-window boundaries."""
    required = {
        "crm_deals": {"upper_deal_id", "included_category_digest", "owner_artifact_id"},
        "crm_activities": {"upper_activity_id", "owner_artifact_id"},
        "openlines_conversations": {
            "discovery_boundary_digest",
            "selected_config_digest",
        },
    }[stream_key]
    if set(source_window) != required:
        raise ValueError(f"{stream_key} source window must contain exactly {sorted(required)}")
    for key, value in source_window.items():
        if key == "owner_artifact_id" and value is None:
            continue
        if not isinstance(value, (str, int)) or isinstance(value, bool) or str(value).strip() == "":
            raise ValueError(f"{stream_key} source window contains an invalid {key}")


def known_owner_refresh_checkpoint(
    membership: KnownOwnerMembershipSet,
    *,
    census_epoch: int,
) -> CheckpointDescriptor:
    return CheckpointDescriptor(
        phase="known_owner_refresh_v1",
        cursor={"last_known_deal_id": None, "census_epoch": census_epoch},
        source_window={
            "known_owner_membership_set_id": membership.membership_set_id,
            "known_owner_set_digest": membership.digest,
            "known_owner_count": len(membership.deal_ids),
        },
        last_committed_record_id=None,
        connector_version="bitrix-crm-known-owner-refresh-v1",
        schema_version=1,
        replay_boundary="exclusive_sorted_known_deal_id",
    )
