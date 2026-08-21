"""CRM deal-count projection maintenance and operator repair."""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic, sleep

from neo4j import ManagedTransaction, Record

from src.graph.client import Neo4jClient
from src.graph.queries.crm_deal_count import (
    BACKFILL_CRM_DEAL_COUNTS_BATCH,
    COMPLETE_CRM_DEAL_COUNT_MIGRATION,
    CRM_DEAL_COUNT_INDEX_CYPHER,
    CRM_DEAL_COUNT_INDEX_NAME,
    CRM_DEAL_COUNT_INVARIANT_COUNTS,
    RECOMPUTE_PERSON_CRM_DEAL_COUNTS,
    RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS,
    SHOW_CRM_DEAL_COUNT_INDEX,
    START_CRM_DEAL_COUNT_MIGRATION,
)

MIGRATION_KEY = "person_crm_deal_count_v1"
DEFAULT_BATCH_SIZE = 500


@dataclass(frozen=True)
class CrmDealCountInvariant:
    invalid_person_count: int
    drifted_person_count: int
    index_online: bool

    @property
    def valid(self) -> bool:
        return (
            self.invalid_person_count == 0 and self.drifted_person_count == 0 and self.index_online
        )


def recompute_person_crm_deal_counts(
    tx: ManagedTransaction, person_ids: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    ids = sorted(set(person_ids))
    if not ids:
        return ()
    return tuple(
        str(record["person_id"])
        for record in tx.run(RECOMPUTE_PERSON_CRM_DEAL_COUNTS, person_ids=ids)
    )


def recompute_source_person_crm_deal_counts(
    tx: ManagedTransaction, source_record_pks: tuple[str, ...] | list[str]
) -> tuple[str, ...]:
    pks = sorted(set(source_record_pks))
    if not pks:
        return ()
    return tuple(
        str(record["person_id"])
        for record in tx.run(RECOMPUTE_SOURCE_PERSON_CRM_DEAL_COUNTS, source_record_pks=pks)
    )


def inspect_crm_deal_count_invariant(client: Neo4jClient) -> CrmDealCountInvariant:
    def _work(tx: ManagedTransaction) -> tuple[int, int]:
        return _parse_counts(tx.run(CRM_DEAL_COUNT_INVARIANT_COUNTS).single())

    invalid, drifted = client.execute_read(_work)
    return CrmDealCountInvariant(invalid, drifted, _index_online(client))


def repair_crm_deal_counts(
    client: Neo4jClient, *, batch_size: int = DEFAULT_BATCH_SIZE, force: bool = True
) -> int:
    if batch_size < 1:
        raise ValueError("CRM deal-count migration batch_size must be positive")
    with client.session() as session:
        session.run(CRM_DEAL_COUNT_INDEX_CYPHER).consume()
    _wait_for_index(client)

    def _start(tx: ManagedTransaction) -> bool:
        record = tx.run(
            START_CRM_DEAL_COUNT_MIGRATION,
            migration_key=MIGRATION_KEY,
            force=force,
        ).single()
        return record is not None and bool(record["completed"])

    if client.execute_write(_start) and not force:
        return 0
    updated_total = 0
    while True:

        def _batch(tx: ManagedTransaction) -> int:
            record = tx.run(
                BACKFILL_CRM_DEAL_COUNTS_BATCH,
                migration_key=MIGRATION_KEY,
                batch_size=batch_size,
            ).single()
            return 0 if record is None else int(record["updated_count"])

        updated = client.execute_write(_batch)
        updated_total += updated
        if updated == 0:
            break

    def _complete(tx: ManagedTransaction) -> tuple[int, int, bool]:
        record = tx.run(
            COMPLETE_CRM_DEAL_COUNT_MIGRATION,
            migration_key=MIGRATION_KEY,
        ).single()
        if record is None:
            return -1, -1, False
        return (
            int(record["invalid_person_count"]),
            int(record["drifted_person_count"]),
            bool(record["completed"]),
        )

    invalid, drifted, completed = client.execute_write(_complete)
    if invalid != 0 or drifted != 0 or not completed:
        raise RuntimeError(
            f"CRM deal-count verification failed: invalid={invalid} drifted={drifted}"
        )
    return updated_total


def _parse_counts(record: Record | None) -> tuple[int, int]:
    if record is None:
        return 0, 0
    return int(record["invalid_person_count"]), int(record["drifted_person_count"])


def _index_online(client: Neo4jClient) -> bool:
    def _work(tx: ManagedTransaction) -> bool:
        record = tx.run(SHOW_CRM_DEAL_COUNT_INDEX, index_name=CRM_DEAL_COUNT_INDEX_NAME).single()
        if record is None:
            return False
        return (
            record["type"] == "RANGE"
            and record["entityType"] == "NODE"
            and record["labelsOrTypes"] == ["Person"]
            and record["properties"] == ["crm_deal_count"]
            and record["state"] == "ONLINE"
            and not record["failureMessage"]
        )

    return client.execute_read(_work)


def _wait_for_index(client: Neo4jClient) -> None:
    deadline = monotonic() + 60
    while monotonic() < deadline:
        if _index_online(client):
            return
        sleep(0.2)
    raise RuntimeError("CRM deal-count index did not become ONLINE with expected metadata")
