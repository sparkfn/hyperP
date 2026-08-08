"""Neo4j repository for Bitrix corrective-generation evidence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_backfill import EXPORT_FROZEN_OWNER_COVERAGE


@dataclass(frozen=True)
class FrozenOwnerRow:
    deal_id: str
    category_id: str
    stage_id: str | None
    source_observation_hash: str


@dataclass(frozen=True)
class FrozenOwnerExport:
    generation_id: str
    source_contract_uuid: str
    configuration_digest: str
    image_digest: str
    boundary_digest: str
    owner_set_digest: str
    rows: tuple[FrozenOwnerRow, ...]


class BitrixBackfillRepository:
    """Read frozen coverage and later manage corrective-generation state."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def export_frozen_owners(self, generation_id: str) -> FrozenOwnerExport:
        if not generation_id.strip():
            raise ValueError("generation_id must be non-empty")

        def _work(tx: ManagedTransaction) -> FrozenOwnerExport:
            records: list[Record] = list(
                tx.run(EXPORT_FROZEN_OWNER_COVERAGE, generation_id=generation_id)
            )
            if not records:
                raise RuntimeError("frozen corrective generation has no in-scope owner coverage")
            boundary = records[0]["boundary_digest"]
            if not isinstance(boundary, str) or not boundary:
                raise RuntimeError("frozen corrective generation omitted its boundary digest")
            rows: list[FrozenOwnerRow] = []
            seen: set[str] = set()
            for record in records:
                if record["boundary_digest"] != boundary:
                    raise RuntimeError("frozen owner coverage has inconsistent boundaries")
                deal_id = _required_str(record["deal_id"], "deal_id")
                if deal_id in seen:
                    raise RuntimeError("frozen owner coverage contains duplicate deal IDs")
                seen.add(deal_id)
                stage = record["stage_id"]
                rows.append(
                    FrozenOwnerRow(
                        deal_id=deal_id,
                        category_id=_required_str(record["category_id"], "category_id"),
                        stage_id=stage if isinstance(stage, str) and stage else None,
                        source_observation_hash=_required_str(
                            record["source_observation_hash"],
                            "source_observation_hash",
                        ),
                    )
                )
            source_contract_uuid = _consistent_string(records, "source_contract_uuid")
            configuration_digest = _consistent_string(records, "configuration_digest")
            image_digest = _consistent_string(records, "image_digest")
            expected_count = records[0]["expected_owner_count"]
            if (
                isinstance(expected_count, bool)
                or not isinstance(expected_count, int)
                or expected_count != len(rows)
            ):
                raise RuntimeError("frozen owner coverage count does not reconcile")
            expected_digest = _consistent_string(records, "expected_owner_set_digest")
            actual_digest = _owner_set_digest(rows)
            if actual_digest != expected_digest:
                raise RuntimeError("frozen owner coverage digest does not reconcile")
            return FrozenOwnerExport(
                generation_id,
                source_contract_uuid,
                configuration_digest,
                image_digest,
                boundary,
                actual_digest,
                tuple(rows),
            )

        return self._client.execute_read(_work)


def _required_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"frozen owner coverage contains an invalid {label}")
    return value


def _consistent_string(records: list[Record], key: str) -> str:
    values = {record[key] for record in records}
    if len(values) != 1:
        raise RuntimeError(f"frozen owner coverage has inconsistent {key}")
    return _required_str(values.pop(), key)


def _owner_set_digest(rows: list[FrozenOwnerRow]) -> str:
    payload = [
        {
            "deal_id": row.deal_id,
            "category_id": row.category_id,
            "stage_id": row.stage_id,
            "source_observation_hash": row.source_observation_hash,
        }
        for row in rows
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"bitrix-frozen-owner-set-v1\x00" + encoded).hexdigest()
