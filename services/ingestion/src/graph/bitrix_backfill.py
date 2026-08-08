"""Neo4j repository for Bitrix corrective-generation evidence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.bitrix_backfill_models import (
    CoverageEntry,
    GenerationProvenance,
    KnownOwnerMembershipSet,
)
from src.bitrix_ingestion_models import BitrixStreamKey, FenceContext
from src.graph.client import Neo4jClient
from src.graph.ingestion_control import assert_active_bitrix_fence
from src.graph.queries.bitrix_backfill import (
    ALLOCATE_BITRIX_BACKFILL_GENERATION,
    ATTACH_BACKFILL_LOGICAL_RUN,
    EXPORT_FROZEN_OWNER_COVERAGE,
    LIST_KNOWN_OWNER_IDS,
    MATERIALIZE_KNOWN_OWNER_SET,
    UPSERT_BITRIX_BACKFILL_COVERAGE,
)


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

    def allocate_generation(
        self,
        generation_id: str,
        provenance: GenerationProvenance,
    ) -> bool:
        if not generation_id.strip():
            raise ValueError("generation_id must be non-empty")
        creation_token = uuid.uuid4().hex

        def _work(tx: ManagedTransaction) -> bool:
            record = tx.run(
                ALLOCATE_BITRIX_BACKFILL_GENERATION,
                generation_id=generation_id,
                repository_sha=provenance.repository_sha,
                image_digest=provenance.image_digest,
                configuration_digest=provenance.configuration_digest,
                source_contract_uuid=provenance.source_contract_uuid,
                boundary_digest=provenance.boundary_digest,
                creation_token=creation_token,
            ).single()
            if record is None:
                raise RuntimeError("generation identity conflicts with existing provenance")
            return record["created"] is True

        return self._client.execute_write(_work)

    def attach_logical_run(
        self,
        *,
        generation_id: str,
        stream_key: BitrixStreamKey,
        logical_run_id: str,
        fence_context: FenceContext,
        boundary_digest: str,
        configuration_digest: str,
    ) -> None:
        def _work(tx: ManagedTransaction) -> None:
            assert_active_bitrix_fence(tx, fence_context)
            record = tx.run(
                ATTACH_BACKFILL_LOGICAL_RUN,
                generation_id=generation_id,
                stream_key=stream_key,
                logical_run_id=logical_run_id,
                boundary_digest=boundary_digest,
                configuration_digest=configuration_digest,
            ).single()
            if record is None:
                raise RuntimeError("corrective generation rejected its child logical run")

        self._client.execute_write(_work)

    def materialize_known_owner_set(
        self,
        *,
        generation_id: str,
        membership_set_id: str,
    ) -> KnownOwnerMembershipSet:
        if not generation_id.strip() or not membership_set_id.strip():
            raise ValueError("generation and membership set IDs must be non-empty")

        def _read(tx: ManagedTransaction) -> tuple[str, ...]:
            deal_ids: list[str] = []
            for record in tx.run(LIST_KNOWN_OWNER_IDS):
                deal_ids.append(_required_str(record["deal_id"], "deal_id"))
            return tuple(deal_ids)

        deal_ids = self._client.execute_read(_read)
        digest = _known_owner_digest(deal_ids)

        def _write(tx: ManagedTransaction) -> KnownOwnerMembershipSet:
            record = tx.run(
                MATERIALIZE_KNOWN_OWNER_SET,
                generation_id=generation_id,
                membership_set_id=membership_set_id,
                deal_ids=list(deal_ids),
                digest=digest,
            ).single()
            if record is None:
                raise RuntimeError("known-owner set changed while it was being sealed")
            count: object = record["member_count"]
            if isinstance(count, bool) or not isinstance(count, int) or count != len(deal_ids):
                raise RuntimeError("known-owner membership count did not reconcile")
            return KnownOwnerMembershipSet(
                generation_id=generation_id,
                membership_set_id=membership_set_id,
                digest=digest,
                deal_ids=deal_ids,
            )

        return self._client.execute_write(_write)

    @staticmethod
    def record_coverage_in_transaction(
        tx: ManagedTransaction,
        *,
        generation_id: str,
        stream_key: BitrixStreamKey,
        fence_context: FenceContext,
        entry: CoverageEntry,
    ) -> None:
        assert_active_bitrix_fence(tx, fence_context)
        record = tx.run(
            UPSERT_BITRIX_BACKFILL_COVERAGE,
            generation_id=generation_id,
            stream_key=stream_key,
            logical_run_id=fence_context.logical_run_id,
            ingest_run_id=fence_context.ingest_run_id,
            attempt_generation=fence_context.attempt_generation,
            stream_generation=fence_context.stream_generation,
            fencing_token=fence_context.fencing_token,
            source_identity=entry.source_identity,
            source_boundary=entry.source_boundary,
            disposition=entry.disposition,
            source_observation_hash=entry.source_observation_hash,
            terminal=entry.terminal,
            deal_id=entry.deal_id,
            scope_state=entry.scope_state,
            entity_key=entry.entity_key,
            category_id=entry.category_id,
            stage_id=entry.stage_id,
            census_epoch=entry.census_epoch,
            detail=entry.detail,
            outcome_digest=entry.outcome_digest,
            creation_token=uuid.uuid4().hex,
        ).single()
        if record is None:
            raise RuntimeError("coverage identity conflicts with an existing terminal outcome")

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


def _known_owner_digest(deal_ids: tuple[str, ...]) -> str:
    encoded = json.dumps(deal_ids, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(b"bitrix-known-owner-set-v1\x00" + encoded).hexdigest()
