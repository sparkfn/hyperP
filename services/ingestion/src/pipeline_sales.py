"""Sales-record ingestion pipeline.

Persists Order / LineItem / Product sub-graphs and links them to the
customer Person when the identity side has been resolved. Sales records
bypass normalization and matching.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import TypedDict, cast

from neo4j import ManagedTransaction

from src.exclusions import ExclusionContext, is_excluded_vehicle_observation
from src.graph import queries
from src.graph.bootstrap import SOURCE_KEY_TO_ENTITY
from src.graph.client import Neo4jClient
from src.matching.vehicle_heuristic import (
    VehicleCandidate,
    build_vehicle_match_result,
    build_vehicle_no_match_result,
    build_vehicle_review_result,
    select_best_vehicle_candidate,
)
from src.models import (
    IngestResult,
    JsonValue,
    SourceRecordEnvelope,
)
from src.normalizers.clean import str_or_none
from src.pipeline_writes import create_review_case_if_needed, persist_match_decision
from src.profile_analysis_dirty import mark_profile_analysis_dirty
from src.raw_payload import decode_raw_payload
from src.record_lifecycle import (
    DuplicateVersion,
    PlannedVersion,
    activate_staged_version,
    load_locked_source_state,
    plan_incoming_version,
    reject_replaced_pending,
)
from src.source_version_keys import encode_source_version_key
from src.vehicle_categories import base_source_key, category_is_vehicle
from src.vehicle_extraction import observations_from_sales_lines
from src.vehicles import (
    normalize_lta_tag,
    normalize_serial_number,
)

logger = logging.getLogger(__name__)


def _staging_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


class SalesPayloadError(ValueError):
    """A permanent sales payload contract violation."""


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


def ingest_sales_record(
    client: Neo4jClient,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str | None,
    exclusion_context: ExclusionContext | None = None,
) -> IngestResult:
    """Full sales-record ingestion in a single write transaction."""
    active_exclusion_context = (
        exclusion_context if exclusion_context is not None else ExclusionContext()
    )

    def _work(tx: ManagedTransaction) -> IngestResult:
        state = load_locked_source_state(tx, envelope.source_system, envelope.source_record_id)
        plan = plan_incoming_version(state, envelope.record_hash)
        if isinstance(plan, DuplicateVersion):
            return IngestResult(
                source_record_id=envelope.source_record_id,
                source_record_pk=plan.source_record_pk,
                skipped_duplicate=True,
                ingest_run_id=ingest_run_id,
            )
        envelope.source_record_version = str(plan.version)
        return _execute(
            tx,
            envelope,
            ingest_run_id=ingest_run_id,
            lifecycle_plan=plan,
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
        raise SalesPayloadError("order missing or not an object")
    order: _OrderPayload = cast(_OrderPayload, order_raw)
    if str_or_none(order_raw.get("source_order_id")) is None:
        raise SalesPayloadError("order.source_order_id missing or invalid")

    # Fundbox stores release_date inside metadata; lift it to top-level.
    metadata_raw = order_raw.get("metadata")
    if "release_date" not in order and isinstance(metadata_raw, dict):
        fundbox_release = metadata_raw.get("release_date")
        if isinstance(fundbox_release, str):
            order["release_date"] = fundbox_release

    line_items_raw = raw.get("line_items")
    if line_items_raw is not None and not isinstance(line_items_raw, list):
        raise SalesPayloadError("line_items must be a list")
    if isinstance(line_items_raw, list):
        for raw_line in line_items_raw:
            if not isinstance(raw_line, dict):
                raise SalesPayloadError("line item must be an object")
            if str_or_none(raw_line.get("source_line_item_id")) is None:
                raise SalesPayloadError("line item source_line_item_id missing or invalid")
            product = raw_line.get("product")
            if not isinstance(product, dict):
                raise SalesPayloadError("line item product missing or invalid")
            if str_or_none(product.get("source_product_id")) is None:
                raise SalesPayloadError("product source_product_id missing or invalid")
    line_items = (
        [cast(_LineItemPayload, li) for li in line_items_raw if isinstance(li, dict)]
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
    return _resolve_customer_person(tx, sales_source_record_pk=source_record_pk)


def _execute(
    tx: ManagedTransaction,
    envelope: SourceRecordEnvelope,
    *,
    ingest_run_id: str | None,
    lifecycle_plan: PlannedVersion,
    exclusion_context: ExclusionContext,
) -> IngestResult:
    source_system_key = envelope.source_system
    if lifecycle_plan.pending_to_reject is not None:
        reject_replaced_pending(tx, lifecycle_plan.pending_to_reject)
    source_record_pk = _create_sales_source_record(
        tx,
        envelope=envelope,
        link_status="pending_customer",
        expected_active_source_record_pk=lifecycle_plan.active_source_record_pk,
    )
    if ingest_run_id is not None:
        tx.run(
            queries.LINK_SOURCE_RECORD_TO_RUN,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )
    try:
        order, line_items, customer_link = _parse_sales_envelope(envelope.raw_payload)
    except SalesPayloadError as exc:
        _skip_sale_permanent(tx, pk=source_record_pk, reason=str(exc))
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )
    if customer_link is None or (
        str_or_none(customer_link.get("identity_source_record_id")) is None
        or str_or_none(customer_link.get("source_system_key")) is None
    ):
        _skip_sale_permanent(
            tx,
            pk=source_record_pk,
            reason="customer_link missing or required fields invalid",
        )
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            ingest_run_id=ingest_run_id,
        )
    source_order_id = str(order["source_order_id"])
    person_id = _resolve_and_link_customer(
        tx,
        source_record_pk=source_record_pk,
        customer_link=customer_link,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
    )
    if person_id is None:
        return IngestResult(
            source_record_id=envelope.source_record_id,
            source_record_pk=source_record_pk,
            is_new_person=False,
            candidate_count=0,
            ingest_run_id=ingest_run_id,
        )
    _finalize_accepted_sale(
        tx,
        sales_pk=source_record_pk,
        person_id=person_id,
        source_system_key=source_system_key,
        source_record_id=envelope.source_record_id,
        raw_payload=envelope.raw_payload,
        observed_at=envelope.observed_at,
        exclusion_context=exclusion_context,
        expected_active_source_record_pk=lifecycle_plan.active_source_record_pk,
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
    expected_active_source_record_pk: str | None,
) -> str:
    source_record_version = envelope.source_record_version
    assert source_record_version is not None, "lifecycle planning must allocate a version"
    rec = tx.run(
        queries.CREATE_SOURCE_RECORD,
        source_system=envelope.source_system,
        entity_key=_entity_key_for(envelope.source_system),
        source_record_id=envelope.source_record_id,
        source_record_version=source_record_version,
        source_version_key=encode_source_version_key(
            envelope.source_system,
            envelope.source_record_id,
            source_record_version,
        ),
        lifecycle_status="pending_review",
        is_latest=False,
        expected_active_source_record_pk=expected_active_source_record_pk,
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


def _stage_sales_review_projections(
    tx: ManagedTransaction,
    *,
    source_record_pk: str,
    source_system_key: str,
    source_record_id: str,
    raw_payload: dict[str, JsonValue],
    exclusion_context: ExclusionContext,
) -> bool:
    """Materialize an isolated, inactive blueprint for API review promotion."""
    order, typed_lines, _ = _parse_sales_envelope(raw_payload)
    source_order_id = str(order.get("source_order_id") or "")
    if not source_order_id:
        return False
    entity_key = _entity_key_for(source_system_key)
    lines: list[dict[str, JsonValue]] = []
    for line_index, line in enumerate(typed_lines):
        product = line.get("product")
        product_values: _ProductPayload = product or {}
        staged_line: dict[str, JsonValue] = {
            "line_index": line_index,
            "source_line_item_id": str(line.get("source_line_item_id") or ""),
            "source_product_id": str(product_values.get("source_product_id") or ""),
            "line_no": line.get("line_no"),
            "quantity": line.get("quantity"),
            "unit_price": line.get("unit_price"),
            "line_total": line.get("line_total"),
            "currency": "SGD",
            "discount_amount": line.get("discount_amount"),
            "tax_amount": line.get("tax_amount"),
            "metadata": json.dumps(line.get("metadata", {}), default=str),
            "product_sku": product_values.get("sku"),
            "product_name": product_values.get("name"),
            "product_display_name": product_values.get("display_name")
            or product_values.get("name"),
            "product_category": product_values.get("category"),
            "product_subcategory": product_values.get("subcategory"),
            "product_manufacturer": product_values.get("manufacturer"),
            "product_attributes": json.dumps(product_values.get("attributes", {}), default=str),
            "product_is_active": product_values.get("is_active", True),
        }
        staged_line["line_hash"] = _staging_hash(staged_line)
        lines.append(staged_line)
    staged_observations: list[dict[str, JsonValue]] = []
    observations = observations_from_sales_lines(
        source_system_key=source_system_key,
        source_record_id=source_record_id,
        observed_at=None,
        lines=cast(list[JsonValue], typed_lines),
    )
    for observation_index, observation in enumerate(observations):
        if is_excluded_vehicle_observation(observation, exclusion_context):
            continue
        staged_observation: dict[str, JsonValue] = {
            "observation_index": observation_index,
            "source_system_key": observation.source_system_key,
            "source_record_id": observation.source_record_id,
            "product_sku": observation.product_sku,
            "product": observation.product,
            "manufacturer": observation.manufacturer,
            "model": observation.model,
            "unit_label": observation.unit_label,
            "lta_tag": observation.lta_tag,
            "normalized_lta_tag": normalize_lta_tag(observation.lta_tag),
            "serial_number": observation.serial_number,
            "normalized_serial_number": normalize_serial_number(observation.serial_number),
            "source_kind": observation.source_kind,
            "observed_at": observation.observed_at,
            "confidence": observation.confidence,
            "quality_flag": observation.quality_flag.value,
            "raw_context": observation.raw_context,
        }
        staged_observation["observation_hash"] = _staging_hash(staged_observation)
        staged_observations.append(staged_observation)
    loyalty = order.get("loyalty") or {}
    staged_order: dict[str, JsonValue] = {
        "source_order_id": source_order_id,
        "entity_key": entity_key,
        "order_no": order.get("order_no"),
        "ordered_at": order.get("ordered_at"),
        "release_date": order.get("release_date"),
        "status": order.get("status"),
        "total_amount": order.get("total_amount"),
        "currency": order.get("currency", "SGD"),
        "item_count": order.get("item_count"),
        "metadata": json.dumps(order.get("metadata", {}), default=str),
        "non_vehicle_lines": json.dumps(
            _build_non_vehicle_lines(source_system_key, cast(list[JsonValue], typed_lines)),
            default=str,
        ),
        "points_used": loyalty.get("points_used"),
        "points_gained": loyalty.get("points_gained"),
        "did_redeem_discount": loyalty.get("did_redeem_discount"),
        "is_purchase_points": loyalty.get("is_purchase_points"),
    }
    order_hash = _staging_hash(staged_order)
    stage_hash = _staging_hash(
        {"order": staged_order, "lines": lines, "observations": staged_observations}
    )
    result = tx.run(
        queries.STAGE_SALES_REVIEW,
        source_record_pk=source_record_pk,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        entity_key=entity_key,
        source_record_id=source_record_id,
        order=staged_order,
        order_hash=order_hash,
        lines=lines,
        observations=staged_observations,
        stage_hash=stage_hash,
    ).single()
    return result is not None


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
        line = raw_line
        product_raw = line.get("product")
        product: dict[str, JsonValue] = product_raw if isinstance(product_raw, dict) else {}
        metadata_raw = line.get("metadata")
        metadata: dict[str, JsonValue] = metadata_raw if isinstance(metadata_raw, dict) else {}
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


def _row_pk_and_ssk(row: Mapping[str, object]) -> tuple[str, str | None]:
    """Extract (source_record_pk, source_system_key) from a FIND_PENDING_CUSTOMER_SALES row.

    ``source_system_key`` is read via the query's FROM_SOURCE traversal; ``str_or_none``
    coerces it to a real string and rejects None/blank/non-string. The caller
    decides whether to mark the sale link_failed (drain) or just skip (propose).
    """
    return str(row["source_record_pk"]), str_or_none(row["source_system_key"])


def _skip_sale_permanent(tx: ManagedTransaction, *, pk: str, reason: str) -> None:
    """Warn once and mark a sales record ``link_failed`` so it stops re-queueing.

    Permanent skips (malformed customer_link, non-string identity keys,
    undecodable raw_payload, missing FROM_SOURCE, corrupt resolved Person) can
    never link, so transitioning to a terminal status stops
    FIND_PENDING_CUSTOMER_SALES from re-scanning the row every drain tick — the
    warning fires exactly once and the pending queue only retries records that
    can still plausibly link.
    """
    logger.warning("Skipping pending sale %s: %s", pk, reason)
    tx.run(queries.MARK_SALES_RECORD_LINK_FAILED, source_record_pk=pk, reason=reason)
    tx.run(queries.MARK_SOURCE_RECORD_LINK_FAILED, source_record_pk=pk, reason=reason)


def _resolve_customer_person_id(
    tx: ManagedTransaction,
    *,
    sales_pk: str,
    identity_source_record_id: str,
    identity_source_system_key: str,
) -> tuple[bool, str | None]:
    """LINK the sale to its identity record and resolve the customer Person.

    Returns ``(resolved, person_id)``: ``(False, None)`` is transient (the
    identity Person isn't linked yet — leave pending and retry); ``(True, None)``
    is permanent (the Person resolved but has no string person_id — corrupt);
    ``(True, person_id)`` is success.
    """
    tx.run(
        queries.LINK_SALES_TO_IDENTITY_RECORD,
        sales_source_record_pk=sales_pk,
        identity_source_record_id=identity_source_record_id,
        source_system_key=identity_source_system_key,
    )
    resolved = tx.run(queries.RESOLVE_SALES_CUSTOMER, sales_source_record_pk=sales_pk).single()
    if resolved is None:
        return False, None
    return True, str_or_none(resolved["person_id"])


def _drain_one_pending_sale(
    tx: ManagedTransaction,
    sales_pk: str,
    source_system_key: str,
    raw_payload: dict[str, JsonValue],
    exclusion_context: ExclusionContext,
    expected_active_source_record_pk: str | None = None,
    source_record_id: str | None = None,
) -> bool:
    """Resolve and link one pending-customer sales record to its customer Person.

    Permanent skips (malformed customer_link, non-string identity keys, or a
    resolved Person missing person_id) mark the record ``link_failed``. The
    transient case — identity Person not yet resolved — stays
    ``pending_customer`` and retries silently; that is why the drain re-runs.
    """
    try:
        _parse_sales_envelope(raw_payload)
    except SalesPayloadError as exc:
        _skip_sale_permanent(tx, pk=sales_pk, reason=str(exc))
        return False
    customer_link = raw_payload.get("customer_link")
    if not isinstance(customer_link, dict):
        _skip_sale_permanent(tx, pk=sales_pk, reason="customer_link missing or not a dict")
        return False
    # customer_link.source_system_key is the IDENTITY source (e.g.
    # ``fundbox``), never the sales source. str_or_none rejects
    # None/blank/non-string; an empty/wrong source would silently fail the
    # identity MATCH and leave the sale pending forever.
    identity_source_record_id = str_or_none(customer_link.get("identity_source_record_id"))
    identity_source_system_key = str_or_none(customer_link.get("source_system_key"))
    if identity_source_record_id is None or identity_source_system_key is None:
        _skip_sale_permanent(
            tx,
            pk=sales_pk,
            reason="identity_source_record_id or source_system_key missing/not a string",
        )
        return False
    resolved, person_id = _resolve_customer_person_id(
        tx,
        sales_pk=sales_pk,
        identity_source_record_id=identity_source_record_id,
        identity_source_system_key=identity_source_system_key,
    )
    if not resolved:
        # Transient: the identity Person isn't linked yet. Leave pending and retry.
        return False
    if person_id is None:
        _skip_sale_permanent(tx, pk=sales_pk, reason="resolved person has no person_id")
        return False
    return _finalize_accepted_sale(
        tx,
        sales_pk=sales_pk,
        person_id=person_id,
        source_system_key=source_system_key,
        source_record_id=source_record_id,
        raw_payload=raw_payload,
        observed_at=(str(raw_payload["observed_at"]) if raw_payload.get("observed_at") else None),
        exclusion_context=exclusion_context,
        expected_active_source_record_pk=expected_active_source_record_pk,
    )


def _finalize_accepted_sale(
    tx: ManagedTransaction,
    *,
    sales_pk: str,
    person_id: str,
    source_system_key: str,
    source_record_id: str | None,
    raw_payload: dict[str, JsonValue],
    observed_at: str | None,
    exclusion_context: ExclusionContext,
    expected_active_source_record_pk: str | None = None,
) -> bool:
    """Write the PURCHASED + vehicle-observation edges for a resolved sale.

    Returns False (leaving the sale pending) if the raw payload carries no
    order id to key the PURCHASED edge on; True once the sale is marked linked.
    """
    try:
        order, typed_lines, _customer_link = _parse_sales_envelope(raw_payload)
    except SalesPayloadError as exc:
        _skip_sale_permanent(tx, pk=sales_pk, reason=str(exc))
        return False
    # ``or ""`` coalesces a present-but-None source_order_id (``.get(k, "")`` would
    # return None for a present-None key, and ``str(None)``="None" is truthy —
    # matching the safe pattern used by the propose path).
    source_order_id = str(order.get("source_order_id") or "")
    if not source_order_id:
        return False

    line_items = cast(list[JsonValue], typed_lines)
    if expected_active_source_record_pk is not None:
        tx.run(
            queries.CLEAR_SUPERSEDED_SALES_LINKS,
            old_source_record_pk=expected_active_source_record_pk,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
        )
    _merge_order(
        tx,
        source_system_key=source_system_key,
        order=order,
        non_vehicle_lines=_build_non_vehicle_lines(source_system_key, line_items),
    )
    tx.run(
        queries.REPLACE_ORDER_LINES,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        source_line_item_ids=[str(line.get("source_line_item_id", "")) for line in typed_lines],
    )
    entity_key = _entity_key_for(source_system_key)
    for line in typed_lines:
        _merge_line_item(
            tx,
            source_system_key=source_system_key,
            source_order_id=source_order_id,
            entity_key=entity_key,
            line=line,
        )

    tx.run(
        queries.LINK_PERSON_PURCHASED_ORDER,
        person_id=person_id,
        source_system_key=source_system_key,
        source_order_id=source_order_id,
        source_record_pk=sales_pk,
    )
    _write_vehicle_observations(
        tx,
        source_system_key=source_system_key,
        source_record_pk=sales_pk,
        source_record_id=source_record_id or sales_pk,
        source_order_id=source_order_id,
        observed_at=observed_at,
        line_items=line_items,
        person_id=person_id,
        exclusion_context=exclusion_context,
    )
    tx.run(queries.MARK_SALES_RECORD_LINKED, source_record_pk=sales_pk)
    if source_record_id is not None:
        activate_staged_version(
            tx,
            source_system=source_system_key,
            source_record_id=source_record_id,
            old_source_record_pk=expected_active_source_record_pk,
            new_source_record_pk=sales_pk,
        )
    mark_profile_analysis_dirty(
        tx,
        source_record_pks=(sales_pk, expected_active_source_record_pk or ""),
        person_ids=(person_id,),
    )
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
    cursor = ""
    while True:

        def _work(tx: ManagedTransaction, _cursor: str = cursor) -> tuple[int, str | None]:
            rows = list(
                tx.run(
                    queries.FIND_PENDING_CUSTOMER_SALES,
                    cursor=_cursor,
                    limit=batch_size,
                )
            )
            if not rows:
                return 0, None
            newly_linked = 0
            for row in rows:
                pk, ssk = _row_pk_and_ssk(row)
                if ssk is None:
                    _skip_sale_permanent(tx, pk=pk, reason="source_system_key missing")
                    continue
                raw_payload = decode_raw_payload(row["raw_payload"])
                if raw_payload is None:
                    _skip_sale_permanent(tx, pk=pk, reason="raw_payload undecodable")
                    continue
                source_record_id = str_or_none(row.get("source_record_id"))
                expected_active_pk = str_or_none(row.get("expected_active_source_record_pk"))
                if source_record_id is None:
                    _skip_sale_permanent(tx, pk=pk, reason="source_record_id missing")
                    continue
                state = load_locked_source_state(tx, ssk, source_record_id)
                if state.pending is None or state.pending.source_record_pk != pk:
                    continue
                if _drain_one_pending_sale(
                    tx,
                    pk,
                    ssk,
                    raw_payload,
                    active_exclusion_context,
                    expected_active_source_record_pk=expected_active_pk,
                    source_record_id=source_record_id,
                ):
                    newly_linked += 1
            return newly_linked, str(rows[-1]["source_record_pk"])

        with client.session() as session:
            newly_linked, next_cursor = session.execute_write(_work)
        linked_count += newly_linked
        if next_cursor is None:
            break
        cursor = next_cursor

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
    source_record_id: str | None = None,
    expected_active_source_record_pk: str | None = None,
    raw_payload: dict[str, JsonValue] | None = None,
    exclusion_context: ExclusionContext | None = None,
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
    if source_record_id is None or raw_payload is None:
        logger.warning(
            "Skipping pending sale %s: lifecycle identity or raw payload missing",
            source_record_pk,
        )
        return False
    try:
        _parse_sales_envelope(raw_payload)
    except SalesPayloadError as exc:
        _skip_sale_permanent(tx, pk=source_record_pk, reason=str(exc))
        return False
    observations = observations_from_sales_lines(
        source_system_key=source_system_key,
        source_record_id=source_record_id,
        observed_at=None,
        lines=(cast(list[JsonValue], raw_payload.get("line_items", []))),
    )
    result = tx.run(
        queries.FIND_VEHICLE_CANDIDATES_FOR_SALES,
        sales_source_record_pk=source_record_pk,
        customer_emails=customer_emails,
        customer_phones=customer_phones,
        customer_nric=customer_nric,
        normalized_serial_numbers=[
            value
            for observation in observations
            if (value := normalize_serial_number(observation.serial_number)) is not None
        ],
        normalized_lta_tags=[
            value
            for observation in observations
            if (value := normalize_lta_tag(observation.lta_tag)) is not None
        ],
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
        if not _stage_sales_review_projections(
            tx,
            source_record_pk=source_record_pk,
            source_system_key=source_system_key,
            source_record_id=source_record_id,
            raw_payload=raw_payload,
            exclusion_context=(
                exclusion_context if exclusion_context is not None else ExclusionContext()
            ),
        ):
            _skip_sale_permanent(tx, pk=source_record_pk, reason="sales review staging failed")
            return False
        review_result = build_vehicle_review_result(candidates)
        decision_id = persist_match_decision(tx, review_result, source_record_pk)
        create_review_case_if_needed(tx, review_result, decision_id)
        tx.run(queries.MARK_SALES_RECORD_PENDING_REVIEW, source_record_pk=source_record_pk)
        return True

    # Single clear candidate → auto-link at VEHICLE_MATCH_AUTO (0.90).
    match_result = build_vehicle_match_result(best)
    persist_match_decision(tx, match_result, source_record_pk)
    _finalize_accepted_sale(
        tx,
        sales_pk=source_record_pk,
        person_id=best.person_id,
        source_system_key=source_system_key,
        source_record_id=source_record_id,
        raw_payload=raw_payload,
        observed_at=None,
        exclusion_context=(
            exclusion_context if exclusion_context is not None else ExclusionContext()
        ),
        expected_active_source_record_pk=expected_active_source_record_pk,
    )
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

    def _get_pending(
        tx: ManagedTransaction,
        cursor: str,
    ) -> tuple[
        list[tuple[str, str, str | None, str | None, dict[str, JsonValue]]],
        str | None,
    ]:
        rows = list(
            tx.run(
                queries.FIND_PENDING_CUSTOMER_SALES,
                cursor=cursor,
                limit=batch_size,
            )
        )
        out: list[tuple[str, str, str | None, str | None, dict[str, JsonValue]]] = []
        for row in rows:
            # Drain runs first and marks permanent skips link_failed, so those
            # rows are excluded from FIND_PENDING here. Defensive skip (no
            # mark) for any row that still lacks a usable ssk/raw_payload — the
            # next drain tick will mark it.
            pk, ssk = _row_pk_and_ssk(row)
            if ssk is None:
                continue
            raw_payload = decode_raw_payload(row["raw_payload"])
            if raw_payload is None:
                continue
            source_record_id = str_or_none(row.get("source_record_id"))
            out.append(
                (
                    pk,
                    ssk,
                    source_record_id,
                    str_or_none(row.get("expected_active_source_record_pk")),
                    raw_payload,
                )
            )
        return out, (str(rows[-1]["source_record_pk"]) if rows else None)

    pending: list[tuple[str, str, str | None, str | None, dict[str, JsonValue]]] = []
    cursor = ""
    while True:
        current_cursor = cursor

        def _read_page(
            tx: ManagedTransaction,
            _cursor: str = current_cursor,
        ) -> tuple[
            list[tuple[str, str, str | None, str | None, dict[str, JsonValue]]],
            str | None,
        ]:
            return _get_pending(tx, _cursor)

        with client.session() as session:
            page, next_cursor = session.execute_write(_read_page)
        pending.extend(page)
        if next_cursor is None:
            break
        cursor = next_cursor

    proposed = 0
    for pk, source_system_key, source_record_id, expected_active_pk, raw_payload in pending:
        order = raw_payload.get("order")
        if not isinstance(order, dict):
            logger.warning("Skipping pending sale %s: raw_payload.order missing", pk)
            continue
        source_order_id = str(order.get("source_order_id") or "")
        if not source_order_id:
            logger.warning("Skipping pending sale %s: source_order_id missing", pk)
            continue
        customer_nric = str_or_none(raw_payload.get("customer_nric"))
        customer_emails = _str_list(raw_payload.get("customer_emails"))
        customer_phones = _str_list(raw_payload.get("customer_phones"))

        def _propose(
            tx: ManagedTransaction,
            _pk: str = pk,
            _ssk: str = source_system_key,
            _oid: str = source_order_id,
            _nric: str | None = customer_nric,
            _emails: list[str] = customer_emails,
            _phones: list[str] = customer_phones,
            _source_record_id: str | None = source_record_id,
            _expected_active_pk: str | None = expected_active_pk,
            _raw_payload: dict[str, JsonValue] = raw_payload,
        ) -> bool:
            if _source_record_id is None:
                logger.warning("Skipping pending sale %s: source_record_id missing", _pk)
                return False
            state = load_locked_source_state(tx, _ssk, _source_record_id)
            if state.pending is None or state.pending.source_record_pk != _pk:
                return False
            return _propose_one_pending_sale(
                tx,
                source_record_pk=_pk,
                source_system_key=_ssk,
                source_order_id=_oid,
                customer_nric=_nric,
                customer_emails=_emails,
                customer_phones=_phones,
                source_record_id=_source_record_id,
                expected_active_source_record_pk=_expected_active_pk,
                raw_payload=_raw_payload,
            )

        with client.session() as session:
            if session.execute_write(_propose):
                proposed += 1
    return proposed
