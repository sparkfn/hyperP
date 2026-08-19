"""Dataset orchestrator: labels + features + serialization (issue #125.2).

Ties together the evidence repository, retrospective label builder, feature
builder, and deterministic SQLite serializer. Produces rows that can be
written to a deterministic SQLite dataset and sealed in the restricted store.

The CLI (python -m src.sales_prediction build-dataset) is the operator
entry point. Operator stdout is aggregates only.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from src.sales_prediction.contracts import (
    DATASET_SCHEMA_VERSION,
    DEFAULT_EXPECTED_MAPPING_VERSION,
    DEFAULT_EXPECTED_POLICY_VERSION,
)
from src.sales_prediction.dataset_serialization import DatasetDigest, write_dataset
from src.sales_prediction.features import FeatureVector, build_feature_vector, derive_sufficiency
from src.sales_prediction.labels import _retrospective_amount_version, build_retrospective_labels
from src.sales_prediction.models import (
    DatasetRow,
    DealVersion,
    LabelEvidence,
    SalesEvidence,
    StageEvent,
)

if TYPE_CHECKING:
    from src.connectors.bitrix_stage_history.artifact_provenance import ArtifactProvenanceInput
    from src.connectors.bitrix_stage_history.artifact_store import LocalRestrictedArtifactStore


@dataclass(frozen=True)
class DatasetBuildResult:
    """Aggregate-only summary of one dataset build, safe to render outside the store."""

    entity_key: str
    row_count: int
    positive_count: int
    negative_count: int
    content_digest: str
    file_sha256: str
    artifact_id: str | None = None


def build_dataset(
    evidence: SalesEvidence,
    entity_keys: tuple[str, ...],
    *,
    expected_mapping_version: str = DEFAULT_EXPECTED_MAPPING_VERSION,
    expected_policy_version: str = DEFAULT_EXPECTED_POLICY_VERSION,
) -> dict[str, list[DatasetRow]]:
    """Build dataset rows for every approved population from accepted evidence.

    Returns a mapping entity_key -> rows. Only positive and negative
    labels produce rows; other labels are excluded from the dataset.
    """
    _ = (expected_mapping_version, expected_policy_version)
    versions_by_parent: defaultdict[tuple[str, str], list[DealVersion]] = defaultdict(list)
    for version in evidence.versions:
        versions_by_parent[version.parent_key].append(version)

    labels = build_retrospective_labels(
        evidence.release,
        evidence.events,
        evidence.versions,
        entity_keys,
        invalid_event_parents=evidence.invalid_event_parents,
    )

    rows_by_entity: dict[str, list[DatasetRow]] = {}
    for label in labels:
        if label.status not in ("positive", "negative"):
            continue
        parent_versions = versions_by_parent.get(label.parent_key, [])
        amount_version = _retrospective_amount_version(parent_versions, label.snapshot_at)
        timeline = _timeline_for_parent(evidence.events, label.parent_key)
        open_event = _open_event_for_snapshot(timeline, label.snapshot_at)
        if open_event is None:
            continue
        episode_index = _episode_index(timeline, label.snapshot_at)
        features = build_feature_vector(
            open_event=open_event,
            timeline=timeline,
            versions=parent_versions,
            amount_version=amount_version,
            snapshot=label.snapshot_at,
            episode_index=episode_index,
        )
        sufficiency = derive_sufficiency(label, features)
        row = _to_dataset_row(label, features, sufficiency)
        rows_by_entity.setdefault(label.entity_key, []).append(row)

    for entity_key in rows_by_entity:
        rows_by_entity[entity_key] = sorted(rows_by_entity[entity_key], key=lambda r: r.row_id)
    return rows_by_entity


def write_and_seal(
    store: LocalRestrictedArtifactStore,
    output_dir: Path,
    entity_key: str,
    rows: list[DatasetRow],
    metadata: dict[str, str],
    *,
    provenance: ArtifactProvenanceInput,
    retention_days: int = 365,
) -> tuple[DatasetDigest, str]:
    """Write one population's dataset to SQLite and seal it in the restricted store."""
    dataset_path = output_dir / f"{entity_key}_dataset.sqlite3"
    digest = write_dataset(dataset_path, metadata, rows)
    with store.begin(artifact_kind="sales-dataset") as session:
        session.write_bytes(
            f"{entity_key}_dataset.sqlite3",
            dataset_path.read_bytes(),
        )
        manifest = session.seal(
            metadata=metadata,
            provenance=provenance,
            retention_expires_at=datetime.now(UTC) + timedelta(days=retention_days),
        )
    return digest, manifest.artifact_id


def summarize_build(
    entity_key: str,
    rows: list[DatasetRow],
    digest: DatasetDigest,
    artifact_id: str | None = None,
) -> DatasetBuildResult:
    """Produce an aggregate-only summary safe to render in CLI output."""
    positive = sum(1 for r in rows if r.label_status == "positive")
    negative = sum(1 for r in rows if r.label_status == "negative")
    return DatasetBuildResult(
        entity_key=entity_key,
        row_count=len(rows),
        positive_count=positive,
        negative_count=negative,
        content_digest=digest.content_digest,
        file_sha256=digest.file_sha256,
        artifact_id=artifact_id,
    )


def dataset_metadata(
    evidence: SalesEvidence,
    entity_key: str,
    expected_mapping_version: str,
    expected_policy_version: str,
) -> dict[str, str]:
    """Canonical metadata for one dataset artifact."""
    return {
        "dataset_schema_version": DATASET_SCHEMA_VERSION,
        "entity_key": entity_key,
        "mapping_version": expected_mapping_version,
        "policy_version": expected_policy_version,
        "evidence_cutoff_at": evidence.release.evidence_cutoff_at.isoformat(),
        "accepted_at": evidence.release.accepted_at.isoformat(),
        "stage_event_count": str(len(evidence.events)),
        "deal_version_count": str(len(evidence.versions)),
        "built_at": datetime.now(UTC).isoformat(),
    }


def _timeline_for_parent(
    events: tuple[StageEvent, ...], parent: tuple[str, str]
) -> list[StageEvent]:
    return sorted(
        (e for e in events if e.parent_key == parent),
        key=lambda e: (e.event_at, e.authority_head_version, e.event_identity),
    )


def _open_event_for_snapshot(timeline: list[StageEvent], snapshot: datetime) -> StageEvent | None:
    """Find the open-episode entry event at snapshot."""
    for event in timeline:
        if event.event_at == snapshot and event.mapped_state == "open":
            return event
    return None


def _episode_index(timeline: list[StageEvent], snapshot: datetime) -> int:
    """Count which open episode this snapshot belongs to (1-based)."""
    index = 0
    prev_state: str | None = None
    for event in timeline:
        if event.mapped_state == "open" and prev_state != "open":
            index += 1
            if event.event_at == snapshot:
                return index
        prev_state = event.mapped_state
    return max(index, 1)


def _to_dataset_row(label: LabelEvidence, features: FeatureVector, sufficiency: str) -> DatasetRow:
    """Assemble one deterministic dataset row from label + features."""
    row_id = _row_id(label)
    label_value = 1 if label.status == "positive" else 0
    return DatasetRow(
        row_id=row_id,
        entity_key=label.entity_key,
        deal_key=f"{label.parent_key[0]}:{label.parent_key[1]}",
        as_of_at=label.snapshot_at.isoformat(),
        month=label.month,
        label=label_value,
        label_status=label.status,
        label_reason=label.reason,
        sufficiency=sufficiency,
        stage_id=features.stage_id,
        category_id=features.category_id,
        source_semantic=features.source_semantic,
        deal_age_days=features.deal_age_days,
        days_since_prev_event=features.days_since_prev_event,
        prior_transition_count=features.prior_transition_count,
        prior_won_count=features.prior_won_count,
        prior_lost_count=features.prior_lost_count,
        episode_index=features.episode_index,
        amount_value=features.amount_value,
        amount_state=features.amount_state,
        currency_status=features.currency_status,
        currency=features.currency,
        amount_known=features.amount_known,
        amount_nonzero=features.amount_nonzero,
        assigned_known=features.assigned_known,
        contact_count=features.contact_count,
        person_linked_at_s=features.person_linked_at_s,
        entity_version_age_days=features.entity_version_age_days,
        month_sin=features.month_sin,
        month_cos=features.month_cos,
        missingness_count=features.missingness_count,
    )


def _row_id(label: LabelEvidence) -> str:
    """Deterministic row id: sha256(entity:parent:snapshot)[:16]."""
    parent_str = f"{label.parent_key[0]}:{label.parent_key[1]}"
    raw = f"{label.entity_key}:{parent_str}:{label.snapshot_at.isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
