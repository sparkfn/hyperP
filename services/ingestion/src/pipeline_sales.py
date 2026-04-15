"""Sales-record ingestion — a parallel track within the same pipeline module.

A sales envelope (``record_type='sales'``) arrives from a connector with
the order header, line items, each line's product reference, and a
``customer_link`` pointer to the identity ``SourceRecord`` that owns this
purchase. This module persists the SourceRecord, writes the Order /
LineItem / Product sub-graph, and — if the identity side is already
resolved — attaches the customer Person via ``PURCHASED``. Otherwise the
sales SourceRecord stays parked with ``link_status='pending_customer'``
until the identity side lands.

Sales records bypass normalization and matching: they carry no
identifiers that should participate in identity resolution.
"""

from __future__ import annotations

import json
import logging
from typing import TypedDict, cast

from neo4j import ManagedTransaction

from src.graph import queries
from src.graph.bootstrap import SOURCE_KEY_TO_ENTITY
from src.graph.client import Neo4jClient
from src.models import (
    IngestResult,
    JsonValue,
    SourceRecordEnvelope,
)

logger = logging.getLogger(__name__)


class _CustomerLink(TypedDict):
    identity_source_record_id: str | None
    source_system_key: str


class _ProductPayload(TypedDict, total=False):
    source_product_id: str
    sku: str | None
    name: str | None
    display_name: str | None
    category: str | None
    subcategory: str | None
    manufacturer: str | None
    is_active: bool
    attributes: dict[str, JsonValue]


class _LineItemPayload(TypedDict, total=False):
    source_line_item_id: str
    line_no: int
    quantity: float | int | None
    unit_price: float | None
    line_total: float | None
    discount_amount: float | None
    tax_amount: float | None
    metadata: dict[str, JsonValue]
    product: _ProductPayload | None


class _OrderPayload(TypedDict, total=False):
    source_order_id: str
    order_no: str | None
    ordered_at: str | None
    status: str | None
    total_amount: float | None
    currency: str
    item_count: int | None
    metadata: dict[str, JsonValue]


def _entity_key_for(source_system_key: str) -> str:
    try:
        return SOURCE_KEY_TO_ENTITY[source_system_key]
    except KeyError as exc:
        raise ValueError(
            f"Unknown source_system_key for entity mapping: {source_system_key!r}"
        ) from exc


def ingest_sales_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str | None,
) -> IngestResult:
    """Full sales-record ingestion in a single write transaction."""

    existing_pk = _check_idempotency(client, envelope)
    if existing_pk is not None:
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=existing_pk,
            skipped_duplicate=True,
        )

    def _work(tx: ManagedTransaction) -> IngestResult:
        return _execute(tx, envelope, ingest_run_id=ingest_run_id)

    with client.session() as session:
        return session.execute_write(_work)


def _check_idempotency(
    client: Neo4jClient, envelope: SourceRecordEnvelope
) -> str | None:
    def _read(tx: ManagedTransaction) -> str | None:
        result = tx.run(
            queries.CHECK_SOURCE_RECORD_EXISTS,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
            record_hash=envelope.record_hash,
        )
        record = result.single()
        return record["source_record_pk"] if record else None

    return client.execute_read(_read)


def _execute(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str | None,
) -> IngestResult:
    raw = envelope.raw_payload
    order_raw = raw.get("order")
    if not isinstance(order_raw, dict):
        raise ValueError("sales envelope missing 'order' payload")
    order = cast(_OrderPayload, order_raw)

    line_items_raw = raw.get("line_items")
    line_items = (
        [cast(_LineItemPayload, li) for li in line_items_raw]
        if isinstance(line_items_raw, list)
        else []
    )

    customer_raw = raw.get("customer_link")
    customer_link: _CustomerLink | None = (
        cast(_CustomerLink, customer_raw) if isinstance(customer_raw, dict) else None
    )

    source_system_key = envelope.source_system
    source_order_id = str(order.get("source_order_id", ""))
    entity_key = _entity_key_for(source_system_key)

    # SourceRecord is always created pending_customer; promoted to linked
    # below once the customer Person has been resolved.
    source_record_pk = _create_sales_source_record(
        tx,
        envelope=envelope,
        link_status="pending_customer",
    )
    if ingest_run_id is not None:
        tx.run(
            queries.LINK_SOURCE_RECORD_TO_RUN,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )

    # 2. Order + SOLD_THROUGH SourceSystem.
    _merge_order(tx, source_system_key=source_system_key, order=order)

    # 3. Products + LineItems (+ SOLD_BY entity for each distinct product).
    for line in line_items:
        _merge_line_item(
            tx,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            entity_key=entity_key,
            line=line,
        )

    # 4. Link to the customer identity record if provided; upgrade link_status
    #    if the customer Person is already resolved.
    person_id: str | None = None
    if customer_link is not None and customer_link.get("identity_source_record_id"):
        _link_sales_to_identity_record(
            tx,
            source_record_pk=source_record_pk,
            customer_link=customer_link,
        )
        person_id = _resolve_customer_person(
            tx, sales_source_record_pk=source_record_pk
        )

    if person_id is not None:
        tx.run(
            queries.LINK_PERSON_PURCHASED_ORDER,
            person_id=person_id,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            source_record_pk=source_record_pk,
        )
        tx.run(queries.MARK_SALES_RECORD_LINKED, source_record_pk=source_record_pk)

    logger.info(
        "Ingested sales record %s -> order %s (person=%s, lines=%d, status=%s)",
        envelope.source_record_id,
        source_order_id,
        person_id,
        len(line_items),
        "linked" if person_id is not None else "pending_customer",
    )

    return IngestResult(
        source_record_id=envelope.source_record_id,
        source_record_pk=source_record_pk,
        person_id=person_id,
        is_new_person=False,
        candidate_count=0,
        match_decision=None,
        ingest_run_id=ingest_run_id,
    )


def _create_sales_source_record(
    tx: ManagedTransaction,
    *,
    envelope: SourceRecordEnvelope,
    link_status: str,
) -> str:
    result = tx.run(
        queries.CREATE_SOURCE_RECORD,
        source_system=envelope.source_system,
        source_record_id=envelope.source_record_id,
        source_record_version=envelope.source_record_version,
        record_type=envelope.record_type.value,
        extraction_confidence=None,
        extraction_method=None,
        conversation_ref=None,
        link_status=link_status,
        observed_at=envelope.observed_at,
        record_hash=envelope.record_hash,
        raw_payload=json.dumps(envelope.raw_payload, default=str),
        normalized_payload=json.dumps({}, default=str),
    )
    record = result.single()
    assert record is not None, "CREATE_SOURCE_RECORD must return a row"
    pk: str = record["source_record_pk"]
    return pk


def _merge_order(
    tx: ManagedTransaction,
    *,
    source_system_key: str,
    order: _OrderPayload,
) -> None:
    tx.run(
        queries.MERGE_ORDER,
        source_system_key=source_system_key,
        source_order_id=str(order.get("source_order_id", "")),
        order_no=order.get("order_no"),
        ordered_at=order.get("ordered_at"),
        status=order.get("status"),
        total_amount=order.get("total_amount"),
        currency=order.get("currency", "SGD"),
        item_count=order.get("item_count"),
        metadata=json.dumps(order.get("metadata", {}), default=str),
    )


def _merge_line_item(
    tx: ManagedTransaction,
    *,
    source_system_key: str,
    source_order_id: str,
    entity_key: str,
    line: _LineItemPayload,
) -> None:
    product = line.get("product")
    if product is None:
        logger.debug(
            "Line item %s has no product reference — skipping",
            line.get("source_line_item_id"),
        )
        return

    tx.run(
        queries.MERGE_PRODUCT,
        source_system_key=source_system_key,
        source_product_id=str(product.get("source_product_id", "")),
        sku=product.get("sku"),
        name=product.get("name"),
        display_name=product.get("display_name") or product.get("name"),
        category=product.get("category"),
        subcategory=product.get("subcategory"),
        manufacturer=product.get("manufacturer"),
        attributes=json.dumps(product.get("attributes", {}), default=str),
        is_active=product.get("is_active", True),
    )
    tx.run(
        queries.LINK_PRODUCT_TO_ENTITY,
        source_system_key=source_system_key,
        source_product_id=str(product.get("source_product_id", "")),
        entity_key=entity_key,
    )

    tx.run(
        queries.MERGE_LINE_ITEM,
        source_system_key=source_system_key,
        source_line_item_id=str(line.get("source_line_item_id", "")),
        source_order_id=source_order_id,
        source_product_id=str(product.get("source_product_id", "")),
        line_no=line.get("line_no"),
        quantity=line.get("quantity"),
        unit_price=line.get("unit_price"),
        line_total=line.get("line_total"),
        currency="SGD",
        discount_amount=line.get("discount_amount"),
        tax_amount=line.get("tax_amount"),
        metadata=json.dumps(line.get("metadata", {}), default=str),
    )


def _link_sales_to_identity_record(
    tx: ManagedTransaction,
    *,
    source_record_pk: str,
    customer_link: _CustomerLink,
) -> None:
    identity_source_record_id = customer_link.get("identity_source_record_id")
    if identity_source_record_id is None:
        return
    tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk=source_record_pk,
        identity_source_record_id=identity_source_record_id,
        source_system_key=customer_link["source_system_key"],
    )


def _resolve_customer_person(
    tx: ManagedTransaction, *, sales_source_record_pk: str
) -> str | None:
    result = tx.run(
        queries.RESOLVE_SALES_CUSTOMER,
        sales_source_record_pk=sales_source_record_pk,
    )
    record = result.single()
    if record is None:
        return None
    person_id: str = record["person_id"]
    return person_id


def drain_pending_customer_sales(
    client: Neo4jClient, *, batch_size: int = 200
) -> int:
    """Re-attempt customer resolution for parked sales SourceRecords.

    Called at the end of an ingest run once the identity side of this
    source system has been processed. Returns the number of records that
    were successfully linked.
    """
    linked_count = 0

    while True:
        def _work(tx: ManagedTransaction) -> int:
            nonlocal linked_count
            result = tx.run(
                queries.FIND_PENDING_CUSTOMER_SALES, limit=batch_size
            )
            rows = list(result)
            if not rows:
                return 0
            newly_linked = 0
            for row in rows:
                sales_pk: str = row["source_record_pk"]
                source_system_key: str = row["source_system_key"]
                raw_payload_str = row["raw_payload"]
                try:
                    raw_payload = json.loads(raw_payload_str)
                except (TypeError, ValueError):
                    continue
                customer_link = raw_payload.get("customer_link") or {}
                identity_source_record_id = customer_link.get(
                    "identity_source_record_id"
                )
                if identity_source_record_id is None:
                    continue

                tx.run(
                    queries.LINK_SALES_TO_IDENTITY_RECORD,
                    sales_source_record_pk=sales_pk,
                    identity_source_record_id=identity_source_record_id,
                    source_system_key=source_system_key,
                )
                resolve = tx.run(
                    queries.RESOLVE_SALES_CUSTOMER,
                    sales_source_record_pk=sales_pk,
                )
                resolved = resolve.single()
                if resolved is None:
                    continue
                person_id: str = resolved["person_id"]

                order_payload = raw_payload.get("order") or {}
                source_order_id = str(order_payload.get("source_order_id", ""))
                if not source_order_id:
                    continue

                tx.run(
                    queries.LINK_PERSON_PURCHASED_ORDER,
                    person_id=person_id,
                    source_system_key=source_system_key,
                    source_order_id=source_order_id,
                    source_record_pk=sales_pk,
                )
                tx.run(
                    queries.MARK_SALES_RECORD_LINKED,
                    source_record_pk=sales_pk,
                )
                newly_linked += 1
            return newly_linked

        with client.session() as session:
            newly_linked = session.execute_write(_work)
        if newly_linked == 0:
            break
        linked_count += newly_linked

    if linked_count:
        logger.info(
            "Drained %d previously pending sales records into linked state",
            linked_count,
        )
    return linked_count
