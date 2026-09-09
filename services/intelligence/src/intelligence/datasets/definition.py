"""Public point-in-time dataset computation facade."""

from __future__ import annotations

from dataclasses import dataclass

from intelligence.datasets.definition_join import activity_evidence, deal_lineage
from intelligence.datasets.definition_rows import deal_version_dispositions, row, select_identity
from intelligence.datasets.models import (
    MAX_DATASET_ROWS,
    AcceptedInputs,
    DatasetRow,
    canonical_digest,
)
from intelligence.datasets.selection import select_version


@dataclass(frozen=True)
class DatasetComputation:
    """Canonical rows and source-evidence dispositions before artifact serialization."""

    rows: tuple[DatasetRow, ...]
    dispositions: tuple[dict[str, object], ...]


def compute(inputs: AcceptedInputs) -> DatasetComputation:
    """Build rows from frozen source snapshots without live reads or mutable-status access."""
    lineage = deal_lineage(inputs)
    if len(lineage.groups) > MAX_DATASET_ROWS:
        raise ValueError("dataset row count exceeds the reviewed dataset bound")
    activities, activity_dispositions = activity_evidence(inputs, lineage)
    rows: list[DatasetRow] = []
    deal_dispositions: list[dict[str, object]] = []
    for source_entity_id, versions in sorted(lineage.groups.items()):
        feature = select_version(versions, inputs.request.feature_cutoff)
        horizon = select_version(versions, inputs.request.label_cutoff)
        identity, reason = select_identity(
            inputs.deals.identities,
            source_entity_id,
            inputs.request.feature_cutoff,
        )
        rows.append(
            row(
                inputs,
                source_entity_id,
                feature,
                horizon,
                identity,
                reason,
                activities.get(source_entity_id, ()),
            )
        )
        deal_dispositions.extend(deal_version_dispositions(versions, feature, horizon))
    rows.sort(key=lambda item: (item.source_instance_id, item.source_entity_id))
    dispositions = tuple(sorted((*activity_dispositions, *deal_dispositions), key=_disposition_key))
    return DatasetComputation(tuple(rows), dispositions)


def content_digest(inputs: AcceptedInputs, computation: DatasetComputation) -> str:
    """Hash logical content only, excluding attempt identity, host paths, and wall clock."""
    from intelligence.datasets.specification import definition, schema

    return canonical_digest(
        {
            "config": inputs.config(),
            "definition": definition(),
            "dispositions": list(computation.dispositions),
            "rows": [item.as_dict() for item in computation.rows],
            "schema": schema(),
        }
    )


def _disposition_key(item: dict[str, object]) -> tuple[str, str]:
    return str(item["kind"]), str(item["source_record_pk"])
