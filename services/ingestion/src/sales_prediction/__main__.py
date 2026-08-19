"""CLI entry point for sales-prediction dataset operations (issue #125.2).

Usage:
    python -m src.sales_prediction build-dataset \
        --entity-keys eko,fundbox,speedzone \
        --output-dir /tmp/sales-datasets

Output is aggregates only - no Person/deal/event IDs, raw payloads, or
restricted boundary values are printed to stdout.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.sales_prediction.dataset import (
    build_dataset,
    dataset_metadata,
    summarize_build,
    write_and_seal,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser = subparsers.add_parser(
        "build-dataset",
        help="Build and seal datasets from accepted CRM evidence",
    )
    build_parser.add_argument(
        "--entity-keys",
        required=True,
        help="Comma-separated entity keys (e.g. eko,fundbox)",
    )
    build_parser.add_argument(
        "--output-dir",
        required=True,
        help="Local directory for SQLite dataset files",
    )
    build_parser.add_argument(
        "--no-seal",
        action="store_true",
        help="Write datasets without sealing to restricted store",
    )

    args = parser.parse_args(argv)

    if args.command == "build-dataset":
        return _build_dataset(args)

    return 1


def _build_dataset(args: argparse.Namespace) -> int:
    from src.config import get_settings
    from src.graph.client import Neo4jClient
    from src.sales_prediction.contracts import (
        DEFAULT_EXPECTED_MAPPING_VERSION,
        DEFAULT_EXPECTED_POLICY_VERSION,
        parse_entity_keys,
    )
    from src.sales_prediction.repository import Neo4jSalesEvidenceRepository

    entity_keys = parse_entity_keys(args.entity_keys)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    client = Neo4jClient(settings)
    repository = Neo4jSalesEvidenceRepository(client)
    evidence = repository.load_evidence(
        expected_mapping_version=DEFAULT_EXPECTED_MAPPING_VERSION,
        expected_policy_version=DEFAULT_EXPECTED_POLICY_VERSION,
    )

    rows_by_entity = build_dataset(evidence, entity_keys)

    results = []
    for entity_key in entity_keys:
        rows = rows_by_entity.get(entity_key, [])
        meta = dataset_metadata(
            evidence,
            entity_key,
            DEFAULT_EXPECTED_MAPPING_VERSION,
            DEFAULT_EXPECTED_POLICY_VERSION,
        )
        if args.no_seal:
            from src.sales_prediction.dataset_serialization import write_dataset

            digest = write_dataset(output_dir / f"{entity_key}_dataset.sqlite3", meta, rows)
            result = summarize_build(entity_key, rows, digest)
        else:
            from src.config import Settings
            from src.connectors.bitrix_stage_history.artifact_provenance import (
                ArtifactProvenanceInput,
            )
            from src.sales_prediction.artifact_support import (
                sales_prediction_store_from_settings,
            )

            settings = Settings()
            store = sales_prediction_store_from_settings(settings)
            provenance = ArtifactProvenanceInput.create(
                source_contract_uuid="00000000-0000-0000-0000-000000000000",
                repository_sha=settings.sales_prediction_repository_sha or "unknown",
                image_digest=settings.sales_prediction_image_digest or "unknown",
                configuration_digest=f"sha256:{'0' * 64}",
                restricted_boundaries={"evidence_cutoff": "bound"},
                counts={"rows": len(rows)},
            )
            try:
                digest, artifact_id = write_and_seal(
                    store,
                    output_dir,
                    entity_key,
                    rows,
                    meta,
                    provenance=provenance,
                )
                result = summarize_build(entity_key, rows, digest, artifact_id)
            finally:
                store.close()

        results.append(
            {
                "entity_key": result.entity_key,
                "row_count": result.row_count,
                "positive_count": result.positive_count,
                "negative_count": result.negative_count,
                "content_digest": result.content_digest[:12],
                "artifact_id": result.artifact_id,
            }
        )

    print(json.dumps({"datasets": results}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
