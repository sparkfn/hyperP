"""Repository access for the durable Bitrix CRM deal-owner scope index."""

from __future__ import annotations

from dataclasses import dataclass

from neo4j import ManagedTransaction, Record

from src.bitrix_ingestion_models import DealScopeState
from src.graph.client import Neo4jClient
from src.graph.queries.bitrix_deal_scope import (
    GET_CURRENT_DEAL_SCOPE,
    UPSERT_IN_SCOPE_DEAL_MEMBERSHIP,
)


@dataclass(frozen=True)
class CurrentDealScope:
    """Current authorization state for one logical Bitrix CRM deal."""

    deal_id: str
    scope_sequence: int
    scope_state: DealScopeState
    entity_key: str | None
    category_id: str | None
    source_record_pk: str | None


class BitrixDealScopeRepository:
    """Persist and retrieve current deal scope without task-local maps."""

    def __init__(self, client: Neo4jClient) -> None:
        self._client = client

    def record_in_scope(
        self,
        *,
        deal_id: str,
        category_id: str,
        entity_key: str,
        source_record_pk: str,
    ) -> CurrentDealScope:
        def _work(tx: ManagedTransaction) -> CurrentDealScope:
            record = tx.run(
                UPSERT_IN_SCOPE_DEAL_MEMBERSHIP,
                deal_id=deal_id,
                category_id=category_id,
                entity_key=entity_key,
                source_record_pk=source_record_pk,
            ).single()
            return _current_scope(record, deal_id, category_id, source_record_pk)

        return self._client.execute_write(_work)

    def get_current(self, deal_id: str) -> CurrentDealScope | None:
        def _work(tx: ManagedTransaction) -> CurrentDealScope | None:
            record = tx.run(GET_CURRENT_DEAL_SCOPE, deal_id=deal_id).single()
            if record is None:
                return None
            category_id = _optional_str(record, "category_id")
            source_record_pk = _optional_str(record, "source_record_pk")
            return _current_scope(record, deal_id, category_id, source_record_pk)

        return self._client.execute_read(_work)


def _current_scope(
    record: Record | None,
    deal_id: str,
    category_id: str | None,
    source_record_pk: str | None,
) -> CurrentDealScope:
    if record is None:
        raise ValueError("Bitrix deal scope write did not return a row")
    sequence = record["scope_sequence"]
    state = record["scope_state"]
    entity_key = _optional_str(record, "entity_key")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("Invalid Bitrix deal scope sequence")
    if state not in {"in_scope", "out_of_scope", "indeterminate"}:
        raise ValueError("Invalid Bitrix deal scope state")
    return CurrentDealScope(
        deal_id=deal_id,
        scope_sequence=sequence,
        scope_state=state,
        entity_key=entity_key,
        category_id=category_id,
        source_record_pk=source_record_pk,
    )


def _optional_str(record: Record, key: str) -> str | None:
    value: object = record[key]
    return value if isinstance(value, str) and value else None
