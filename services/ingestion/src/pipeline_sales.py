"""Sales-record ingestion pipeline.

Persists Order / LineItem / Product sub-graphs and links them to the
customer Person when the identity side has been resolved. Sales records
bypass normalization and matching.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TypedDict, cast

from neo4j import ManagedTransaction

from src.exclusions import ExclusionContext, is_excluded_vehicle_observation
from src.graph import queries
from src.graph.bootstrap import SOURCE_KEY_TO_ENTITY
from src.graph.client import Neo4jClient
from src.normalizers.clean import str_or_none
from src.vehicle_categories import base_source_key, category_is_vehicle
from src.vehicle_extraction import observations_from_sales_lines
from src.vehicles import (
    normalize_lta_tag,
    normalize_serial_number,
)
from src.matching.vehicle_heuristic import (
    VEHICLE_MATCH_AUTO,
    VehicleCandidate,
    build_vehicle_match_result,
    build_vehicle_no_match_result,
    build_vehicle_review_result,
    select_best_vehicle_candidate,
)
from src.models import (
    IngestResult,
    JsonValue,
    QualityFlag,
    SourceRecordEnvelope,
)
from src.pipeline_writes import create_review_case_if_needed, persist_match_decision

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
    release_date: str | None
    status: str | None
    total_amount: float | None
    currency: str
    item_count: int | None
    metadata: dict[str, JsonValue]
    loyalty: dict[str, JsonValue]


def _entity_key_for(source_system_key: str) -> str:
    try:
        return SOURCE_KEY_TO_ENTITY[source_system_key]
    except KeyError as exc:
        raise ValueError(f"Unknown source_system_key: {source_system_key!r}") from exc


def _latest_source_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
) -> tuple[str | None, str | None, int]:
    def _read(tx: ManagedTransaction) -> tuple[str | None, str | None, int]:
        rec = tx.run(
            queries.GET_LATEST_SOURCE_RECORD,
            source_system=envelope.source_system,
            source_record_id=envelope.source_record_id,
        ).single()
        if rec is None:
            return None, None, 1
        version = int(rec["source_record_version"])
        return str(rec["source_record_pk"]), str(rec["record_hash"]), version + 1

    return client.execute_read(_read)


def ingest_sales_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str | None,
    exclusion_context: ExclusionContext | None = None,
) -> IngestResult:
    """Full sales-record ingestion in a single write transaction."""
    latest_pk, latest_hash, next_version = _latest_source_record(client, envelope)
    if latest_pk is not None and latest_hash == envelope.record_hash:
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=latest_pk,
            skipped_duplicate=True,
        )
    envelope.source_record_version = str(next_version)
    active_exclusion_context = (
        exclusion_context if exclusion_context is not None else ExclusionContext()
    )

    def _work(tx: ManagedTransaction) -> IngestResult:
        return _execute(
            tx,
            envelope,
            ingest_run_id=ingest_run_id,
            previous_source_record_pk=latest_pk,
            exclusion_context=active_exclusion_context,
        )

    with client.session() as session:
        return session.execute_write(_work)


def _parse_sales_envelope(
    raw: dict[str, JsonValue],
) -> tuple[_OrderPayload, list[_LineItemPayload], _CustomerLink | None]:
    """Extract and cast the three payload sections from a raw sales envelope."""
    order_raw = raw.get("order")
    if not isinstance(order_raw, dict):
        raise ValueError("sales envelope missing 'order' payload")
    order: _OrderPayload = cast(_OrderPayload, order_raw)

    # Fundbox stores release_date inside metadata; lift it to top-level.
    metadata_raw = order_raw.get("metadata")
    if "release_date" not in order and isinstance(metadata_raw, dict):
        fundbox_release = metadata_raw.get("release_date")
        if isinstance(fundbox_release, str):
            order["release_date"] = fundbox_release

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
    return order, line_items, customer_link


def _resolve_and_link_customer(
    tx: ManagedTransaction,
    *,
    source_record_pk: str,
    customer_link: _CustomerLink | None,
    source_system_key: str,
    source_order_id: str,
) -> str | None:
    """Attempt to link sales record to the customer Person. Returns person_id."""
    if customer_link is None or not customer_link.get("identity_source_record_id"):
        return None
    _link_sales_to_identity_record(
        tx,
        source_record_pk=source_record_pk,
        customer_link=customer_link,
    )
    person_id = _resolve_customer_person(tx, sales_source_record_pk=source_record_pk)
    if person_id is not None:
        tx.run(
            queries.LINK_PERSON_PURCHASED_ORDER,
            person_id=person_id,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            source_record_pk=source_record_pk,
        )
        tx.run(queries.MARK_SALES_RECORD_LINKED, source_record_pk=source_record_pk)
    return person_id


def _execute(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str | None,
    previous_source_record_pk: str | None,
    exclusion_context: ExclusionContext,
) -> IngestResult:
    order, line_items, customer_link = _parse_sales_envelope(envelope.raw_payload)
    source_system_key = envelope.source_system
    source_order_id = str(order.get("source_order_id", ""))
    entity_key = _entity_key_for(source_system_key)

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
    if previous_source_record_pk is not None:
        tx.run(
            queries.SUPERSEDE_SOURCE_RECORD,
            old_source_record_pk=previous_source_record_pk,
            new_source_record_pk=source_record_pk,
        )
        tx.run(
            queries.CLEAR_SUPERSEDED_SALES_LINKS,
            old_source_record_pk=previous_source_record_pk,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
        )

    non_vehicle_lines = _build_non_vehicle_lines(source_system_key, cast(list[JsonValue], line_items))
    _merge_order(
        tx,
        source_system_key=source_system_key,
        order=order,
        non_vehicle_lines=non_vehicle_lines,
    )
    for line in line_items:
        _merge_line_item(
            tx,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            entity_key=entity_key,
            line=line,
        )

    person_id = _resolve_and_link_customer(
        tx,
        source_record_pk=source_record_pk,
        customer_link=customer_link,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
    )
    _write_vehicle_observations(
        tx,
        source_system_key=source_system_key,
        source_record_pk=source_record_pk,
        source_record_id=envelope.source_record_id,
        source_order_id=source_order_id,
        observed_at=envelope.observed_at,
        line_items=cast(list[JsonValue], line_items),
        person_id=person_id,
        exclusion_context=exclusion_context,
    )

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
    rec = tx.run(
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
    ).single()
    assert rec is not None, "CREATE_SOURCE_RECORD must return a row"
    pk: str = rec["source_record_pk"]
    return pk


def _write_vehicle_observations(
    tx: ManagedTransaction,
    *,
    source_system_key: str,
    source_record_pk: str,
    source_record_id: str,
    source_order_id: str,
    observed_at: str | None,
    line_items: list[JsonValue],
    person_id: str | None,
    exclusion_context: ExclusionContext,
) -> None:
    """Upsert Vehicle nodes for vehicle-category lines and wire Order/Person edges.

    Only lines whose product category is a vehicle category for the source AND
    that carry a serial or LTA identifier produce a Vehicle here (see
    ``observations_from_sales_lines``). Vehicle-category lines lacking a serial
    or LTA, and all non-vehicle lines, are carried on the Order via
    ``non_vehicle_lines`` (assembled in ``_execute``) and do NOT produce a
    Vehicle.
    """
    observations = observations_from_sales_lines(
        source_system_key=source_system_key,
        source_record_id=source_record_id,
        observed_at=observed_at,
        lines=line_items,
    )
    for observation in observations:
        if is_excluded_vehicle_observation(observation, exclusion_context):
            continue
        result = tx.run(
            queries.UPSERT_VEHICLE,
            source_system_key=observation.source_system_key,
            product_sku=observation.product_sku,
            product=observation.product,
            manufacturer=observation.manufacturer,
            model=observation.model,
            lta_tag=observation.lta_tag,
            normalized_lta_tag=normalize_lta_tag(observation.lta_tag),
            serial_number=observation.serial_number,
            normalized_serial_number=normalize_serial_number(observation.serial_number),
            observed_at=observation.observed_at,
        ).single()
        if result is None:
            continue
        vehicle_id = str(result["vehicle_id"])
        conflict = bool(result["conflict"])
        if conflict:
            logger.debug(
                "Vehicle %s upserted with identifier conflict for order %s (source=%s)",
                vehicle_id,
                source_order_id,
                source_system_key,
            )
        tx.run(
            queries.LINK_ORDER_INVOLVES_VEHICLE,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            source_record_pk=source_record_pk,
            vehicle_id=vehicle_id,
            raw_context=observation.raw_context,
            observed_at=observation.observed_at,
            confidence=observation.confidence,
            quality_flag=observation.quality_flag.value,
        )
        if person_id is not None:
            tx.run(
                queries.LINK_PERSON_BOUGHT_VEHICLE,
                person_id=person_id,
                vehicle_id=vehicle_id,
                source_system_key=source_system_key,
                source_order_id=source_order_id,
                source_record_pk=source_record_pk,
                is_active=True,
                raw_context=observation.raw_context,
                observed_at=observation.observed_at,
                confidence=observation.confidence,
                quality_flag=observation.quality_flag.value,
            )


def _str_list(value: object) -> list[str]:
    """Coerce a raw_payload field into a ``list[str]`` of non-empty strings.

    Accepts a list of strings; any non-string or empty items are dropped. A
    bare string is treated as a single-element list. Anything else → ``[]``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _build_non_vehicle_lines(
    source_system_key: str,
    line_items: list[JsonValue],
) -> list[dict[str, JsonValue]]:
    """Assemble ``Order.non_vehicle_lines`` from non-vehicle sales lines.

    A line is a *vehicle* line (and produces a Vehicle in
    ``_write_vehicle_observations``) when its product category is a vehicle
    category for the source AND it carries a serial or LTA identifier. Every
    other line — including vehicle-category lines that lack a serial/lta and
    all non-vehicle-category lines — is carried on the Order as a non-vehicle
    line detail dict.
    """
    base_key = base_source_key(source_system_key)
    non_vehicle_lines: list[dict[str, JsonValue]] = []
    for raw_line in line_items:
        if not isinstance(raw_line, dict):
            continue
        line = cast(dict[str, JsonValue], raw_line)
        product_raw = line.get("product")
        product: dict[str, JsonValue] = (
            cast(dict[str, JsonValue], product_raw)
            if isinstance(product_raw, dict)
            else {}
        )
        metadata_raw = line.get("metadata")
        metadata: dict[str, JsonValue] = (
            cast(dict[str, JsonValue], metadata_raw)
            if isinstance(metadata_raw, dict)
            else {}
        )
        category = str_or_none(product.get("category"))
        serial_number = (
            str_or_none(metadata.get("serial_number"))
            or str_or_none(metadata.get("serial_no"))
            or str_or_none(metadata.get("serialnumber"))
        )
        lta_tag = str_or_none(metadata.get("lta_tag"))
        has_identifier = (
            normalize_lta_tag(lta_tag) is not None
            or normalize_serial_number(serial_number) is not None
        )
        if category_is_vehicle(base_key, category) and has_identifier:
            continue
        non_vehicle_lines.append(
            {
                "source_line_item_id": line.get("source_line_item_id")
                or line.get("source_line_id"),
                "sku": product.get("sku") or product.get("item_number"),
                "product_name": product.get("display_name") or product.get("name"),
                "category": category,
                "manufacturer": str_or_none(product.get("manufacturer")),
                "model": str_or_none(product.get("model")),
                "serial_number": serial_number,
                "lta_tag": lta_tag,
                "quantity": line.get("quantity"),
                "unit_price": line.get("unit_price"),
                "line_total": line.get("line_total"),
                "merchant": str_or_none(metadata.get("merchant")),
            }
        )
    return non_vehicle_lines


def _merge_order(
    tx: ManagedTransaction,
    *,
    source_system_key: str,
    order: _OrderPayload,
    non_vehicle_lines: list[dict[str, JsonValue]],
) -> None:
    loyalty = order.get("loyalty") or {}
    tx.run(
        queries.MERGE_ORDER,
        source_system_key=source_system_key,
        source_order_id=str(order.get("source_order_id", "")),
        order_no=order.get("order_no"),
        ordered_at=order.get("ordered_at"),
        release_date=order.get("release_date"),
        status=order.get("status"),
        total_amount=order.get("total_amount"),
        currency=order.get("currency", "SGD"),
        item_count=order.get("item_count"),
        metadata=json.dumps(order.get("metadata", {}), default=str),
        # Neo4j cannot store LIST<MAP> node properties; store as JSON string.
        # The API mapper (Task 9) json.loads() this back to a list.
        non_vehicle_lines=json.dumps(non_vehicle_lines, default=str),
        points_used=loyalty.get("points_used"),
        points_gained=loyalty.get("points_gained"),
        did_redeem_discount=loyalty.get("did_redeem_discount"),
        is_purchase_points=loyalty.get("is_purchase_points"),
    )


def _merge_product(
    tx: ManagedTransaction,
    source_system_key: str,
    entity_key: str,
    product: _ProductPayload,
) -> None:
    """MERGE a Product node and wire it to its Entity via SOLD_BY."""
    source_product_id = str(product.get("source_product_id", ""))
    tx.run(
        queries.MERGE_PRODUCT,
        source_system_key=source_system_key,
        source_product_id=source_product_id,
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
        source_product_id=source_product_id,
        entity_key=entity_key,
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
        return
    _merge_product(tx, source_system_key, entity_key, product)
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


def _resolve_customer_person(tx: ManagedTransaction, *, sales_source_record_pk: str) -> str | None:
    result = tx.run(
        queries.RESOLVE_SALES_CUSTOMER,
        sales_source_record_pk=sales_source_record_pk,
    )
    record = result.single()
    if record is None:
        return None
    person_id: str = record["person_id"]
    return person_id


def _drain_one_pending_sale(
    tx: ManagedTransaction,
    sales_pk: str,
    source_system_key: str,
    raw_payload: dict[str, JsonValue],
    exclusion_context: ExclusionContext,
) -> bool:
    """Try to resolve and link a single pending-customer sales record."""
    customer_link = raw_payload.get("customer_link") or {}
    identity_source_record_id = (
        customer_link.get("identity_source_record_id") if isinstance(customer_link, dict) else None
    )
    if identity_source_record_id is None:
        return False

    tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk=sales_pk,
        identity_source_record_id=identity_source_record_id,
        source_system_key=source_system_key,
    )
    resolved = tx.run(queries.RESOLVE_SALES_CUSTOMER, sales_source_record_pk=sales_pk).single()
    if resolved is None:
        return False
    person_id: str = resolved["person_id"]

    order_payload = raw_payload.get("order") or {}
    source_order_id = str(
        order_payload.get("source_order_id", "") if isinstance(order_payload, dict) else ""
    )
    if not source_order_id:
        return False

    tx.run(
        queries.LINK_PERSON_PURCHASED_ORDER,
        person_id=person_id,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        source_record_pk=sales_pk,
    )
    line_items_raw = raw_payload.get("line_items")
    line_items = line_items_raw if isinstance(line_items_raw, list) else []
    source_record_id = str(raw_payload.get("source_record_id") or source_order_id)
    observed_at = str(raw_payload.get("observed_at")) if raw_payload.get("observed_at") else None
    _write_vehicle_observations(
        tx,
        source_system_key=source_system_key,
        source_record_pk=sales_pk,
        source_record_id=source_record_id,
        source_order_id=source_order_id,
        observed_at=observed_at,
        line_items=line_items,
        person_id=person_id,
        exclusion_context=exclusion_context,
    )
    tx.run(queries.MARK_SALES_RECORD_LINKED, source_record_pk=sales_pk)
    return True


def drain_pending_customer_sales(
    client: Neo4jClient,
    *,
    batch_size: int = 200,
    exclusion_context: ExclusionContext | None = None,
) -> int:
    """Re-attempt customer resolution for parked sales SourceRecords."""
    linked_count = 0
    active_exclusion_context = (
        exclusion_context if exclusion_context is not None else ExclusionContext()
    )
    while True:

        def _work(tx: ManagedTransaction) -> int:
            rows = list(tx.run(queries.FIND_PENDING_CUSTOMER_SALES, limit=batch_size))
            if not rows:
                return 0
            newly_linked = 0
            for row in rows:
                try:
                    raw_payload = json.loads(row["raw_payload"])
                except (TypeError, ValueError):
                    continue
                if _drain_one_pending_sale(
                    tx,
                    row["source_record_pk"],
                    row["source_system_key"],
                    raw_payload,
                    active_exclusion_context,
                ):
                    newly_linked += 1
            return newly_linked

        with client.session() as session:
            newly_linked = session.execute_write(_work)
        if newly_linked == 0:
            break
        linked_count += newly_linked

    if linked_count:
        logger.info("Drained %d pending sales records into linked state", linked_count)
    return linked_count


def _propose_one_pending_sale(
    tx: ManagedTransaction,
    *,
    source_record_pk: str,
    source_system_key: str,
    source_order_id: str,
    customer_nric: str | None,
    customer_emails: list[str],
    customer_phones: list[str],
) -> bool:
    """Try to resolve one pending-customer sales record via the Vehicle heuristic.

    Four outcomes (see SDD Task 6):
      * No candidates → return False (sale stays ``pending_customer``).
      * Best candidate NRIC-blocked → ``NO_MATCH`` decision recorded, return True.
      * Multiple distinct candidate persons → ``REVIEW`` decision + ReviewCase,
        sale moves to ``pending_review``, return True.
      * Single clear candidate → ``MERGE`` (auto-merge, 0.90) decision recorded,
        PURCHASED + BOUGHT_VEHICLE edges written, sale marked ``linked``,
        return True.
    """
    result = tx.run(
        queries.FIND_VEHICLE_CANDIDATES_FOR_SALES,
        sales_source_record_pk=source_record_pk,
        customer_emails=customer_emails,
        customer_phones=customer_phones,
        customer_nric=customer_nric,
    )
    candidates: list[VehicleCandidate] = [
        VehicleCandidate(
            person_id=str(row["person_id"]),
            vehicle_id=str(row["vehicle_id"]),
            rel_type=str(row["rel_type"]),
            is_active=bool(row.get("is_active", False)),
            conflict_flag=bool(row.get("conflict_flag", False)),
            last_confirmed_at=(
                str(row["last_confirmed_at"]) if row["last_confirmed_at"] is not None else None
            ),
            contact_channels=[
                str(ch) for ch in (row.get("contact_channels") or []) if ch is not None
            ],
            nric_blocked=bool(row.get("nric_blocked", False)),
        )
        for row in result
    ]
    if not candidates:
        return False
    best = select_best_vehicle_candidate(candidates)
    if best is None:
        return False

    # NRIC anti-match: record NO_MATCH for this pair and stop. The sale is NOT
    # linked; the decision prevents re-propose against the same person on the
    # next run (the MatchDecision is persisted against the source record).
    # If the best candidate is NRIC-blocked, drop it from the candidate pool
    # and re-select from the remainder — a blocked Person must never carry
    # evidence edges from this sale. If no unblocked candidate remains, fall
    # through to NO_MATCH against the last-blocked candidate.
    if best.nric_blocked:
        blocked_ids = {best.person_id}
        unblocked = [c for c in candidates if c.person_id not in blocked_ids]
        next_best = select_best_vehicle_candidate(unblocked)
        if next_best is None:
            no_match_result = build_vehicle_no_match_result(best)
            persist_match_decision(tx, no_match_result, source_record_pk)
            return True
        best = next_best
        candidates = unblocked

    # Multiple distinct candidate persons → review band. Do not auto-link; the
    # best candidate is the primary, every other distinct person is carried on
    # ``additional_linked_person_ids`` so their evidence is linked without merge.
    person_ids = {c.person_id for c in candidates}
    if len(person_ids) >= 2:
        review_result = build_vehicle_review_result(candidates)
        decision_id = persist_match_decision(tx, review_result, source_record_pk)
        create_review_case_if_needed(tx, review_result, decision_id)
        tx.run(queries.MARK_SALES_RECORD_PENDING_REVIEW, source_record_pk=source_record_pk)
        return True

    # Single clear candidate → auto-link at VEHICLE_MATCH_AUTO (0.90).
    match_result = build_vehicle_match_result(best)
    persist_match_decision(tx, match_result, source_record_pk)
    tx.run(
        queries.LINK_PERSON_PURCHASED_ORDER,
        person_id=best.person_id,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        source_record_pk=source_record_pk,
    )
    tx.run(
        queries.LINK_PERSON_BOUGHT_VEHICLE,
        person_id=best.person_id,
        vehicle_id=best.vehicle_id,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        source_record_pk=source_record_pk,
        is_active=True,
        raw_context=None,
        observed_at=datetime.now(UTC).isoformat(),
        confidence=VEHICLE_MATCH_AUTO,
        quality_flag=QualityFlag.VALID.value,
    )
    tx.run(queries.MARK_SALES_RECORD_LINKED, source_record_pk=source_record_pk)
    return True


def propose_vehicle_matches_for_pending_sales(
    client: Neo4jClient,
    *,
    batch_size: int = 200,
) -> int:
    """Run the Vehicle matching heuristic over all pending-customer sales records.

    Single-pass — records with no candidates stay ``pending_customer`` and are
    retried on the next ingestion run. Returns the count of records that
    received a decision (auto-link, review, or no-match).
    """

    def _get_pending(tx: ManagedTransaction) -> list[tuple[str, str, dict[str, JsonValue]]]:
        rows = list(tx.run(queries.FIND_PENDING_CUSTOMER_SALES, limit=batch_size))
        out: list[tuple[str, str, dict[str, JsonValue]]] = []
        for row in rows:
            pk = str(row["source_record_pk"])
            ssk = str(row["source_system_key"])
            raw = row["raw_payload"]
            out.append((pk, ssk, raw if isinstance(raw, dict) else {}))
        return out

    with client.session() as session:
        pending = session.execute_write(_get_pending)

    proposed = 0
    for pk, source_system_key, raw_payload in pending:
        order = raw_payload.get("order")
        if not isinstance(order, dict):
            logger.warning(
                "Skipping pending sale %s: raw_payload.order missing", pk
            )
            continue
        source_order_id = str(order.get("source_order_id") or "")
        if not source_order_id:
            logger.warning("Skipping pending sale %s: source_order_id missing", pk)
            continue
        customer_nric = str_or_none(raw_payload.get("customer_nric"))
        customer_emails = _str_list(raw_payload.get("customer_emails"))
        customer_phones = _str_list(raw_payload.get("customer_phones"))

        def _propose(tx: ManagedTransaction, _pk: str = pk) -> bool:
            return _propose_one_pending_sale(
                tx,
                source_record_pk=_pk,
                source_system_key=source_system_key,
                source_order_id=source_order_id,
                customer_nric=customer_nric,
                customer_emails=customer_emails,
                customer_phones=customer_phones,
            )

        with client.session() as session:
            if session.execute_write(_propose):
                proposed += 1
    return proposed
