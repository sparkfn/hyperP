"""Cypher for the sales sub-graph: Order, LineItem, Product, and edges.

Writes are driven by a sales ``SourceRecord`` (``record_type='sales'``).
The connector emits one envelope per Order; the envelope's normalized
payload carries the order header, an ordered list of line items, and each
line's product reference. The pipeline calls these queries in order:

1. ``MERGE_PRODUCT`` (once per distinct product referenced in the order),
2. ``MERGE_ORDER``,
3. ``MERGE_LINE_ITEM`` (once per line),
4. ``LINK_ORDER_TO_SYSTEM`` (SOLD_THROUGH edge),
5. ``LINK_PRODUCT_TO_ENTITY`` (SOLD_BY edge),
6. ``LINK_PERSON_PURCHASED_ORDER`` — only when the customer is resolved.

All writes are idempotent on ``(source_system_key, source_*_id)``.
"""

from __future__ import annotations

STAGE_SALES_REVIEW = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk,
                        lifecycle_status: 'pending_review', record_type: 'sales'})
MATCH (sr)-[:FROM_SOURCE]->(source:SourceSystem {source_key: $source_system_key})
WHERE sr.source_record_id = $source_record_id
SET sr.sales_stage_lock_version = coalesce(sr.sales_stage_lock_version, 0) + 1
MERGE (stage:StagedSalesOrder {stage_order_key: sr.source_record_pk})
SET stage.lock_version = coalesce(stage.lock_version, 0) + 1,
    stage.source_record_pk = sr.source_record_pk,
    stage.source_system_key = source.source_key,
    stage.entity_key = $entity_key,
    stage.source_order_id = $source_order_id,
    stage.order_no = $order.order_no,
    stage.ordered_at = $order.ordered_at,
    stage.release_date = $order.release_date,
    stage.status = $order.status,
    stage.total_amount = $order.total_amount,
    stage.currency = $order.currency,
    stage.item_count = $order.item_count,
    stage.metadata = $order.metadata,
    stage.non_vehicle_lines = $order.non_vehicle_lines,
    stage.points_used = $order.points_used,
    stage.points_gained = $order.points_gained,
    stage.did_redeem_discount = $order.did_redeem_discount,
    stage.is_purchase_points = $order.is_purchase_points,
    stage.stage_hash = $stage_hash,
    stage.order_hash = $order_hash,
    stage.expected_line_count = size($lines),
    stage.expected_observation_count = size($observations),
    stage.updated_at = datetime()
WITH sr, stage
OPTIONAL MATCH (stage)-[:STAGED_CONTAINS]->(old:StagedSalesLine)
DETACH DELETE old
WITH DISTINCT sr, stage
OPTIONAL MATCH (stage)-[:STAGED_OBSERVATION]->(old:StagedSalesVehicleObservation)
DETACH DELETE old
WITH DISTINCT sr, stage
UNWIND $lines AS line
CREATE (staged_line:StagedSalesLine {stage_line_key: sr.source_record_pk + ':' +
  toString(line.line_index)})
SET staged_line.line_index = line.line_index,
    staged_line.line_hash = line.line_hash,
    staged_line.source_line_item_id = line.source_line_item_id,
    staged_line.source_product_id = line.source_product_id,
    staged_line.line_no = line.line_no,
    staged_line.quantity = line.quantity,
    staged_line.unit_price = line.unit_price,
    staged_line.line_total = line.line_total,
    staged_line.currency = line.currency,
    staged_line.discount_amount = line.discount_amount,
    staged_line.tax_amount = line.tax_amount,
    staged_line.metadata = line.metadata,
    staged_line.product_sku = line.product_sku,
    staged_line.product_name = line.product_name,
    staged_line.product_display_name = line.product_display_name,
    staged_line.product_category = line.product_category,
    staged_line.product_subcategory = line.product_subcategory,
    staged_line.product_manufacturer = line.product_manufacturer,
    staged_line.product_attributes = line.product_attributes,
    staged_line.product_is_active = line.product_is_active
MERGE (stage)-[:STAGED_CONTAINS]->(staged_line)
WITH DISTINCT sr, stage
UNWIND $observations AS observation
CREATE (staged_observation:StagedSalesVehicleObservation {
  stage_observation_key: sr.source_record_pk + ':' + toString(observation.observation_index)
})
SET staged_observation.observation_index = observation.observation_index,
    staged_observation.observation_hash = observation.observation_hash,
    staged_observation.source_system_key = source.source_key,
    staged_observation.source_record_id = sr.source_record_id,
    staged_observation.product_sku = observation.product_sku,
    staged_observation.product = observation.product,
    staged_observation.manufacturer = observation.manufacturer,
    staged_observation.model = observation.model,
    staged_observation.unit_label = observation.unit_label,
    staged_observation.lta_tag = observation.lta_tag,
    staged_observation.normalized_lta_tag = observation.normalized_lta_tag,
    staged_observation.serial_number = observation.serial_number,
    staged_observation.normalized_serial_number = observation.normalized_serial_number,
    staged_observation.source_kind = observation.source_kind,
    staged_observation.observed_at = observation.observed_at,
    staged_observation.confidence = observation.confidence,
    staged_observation.quality_flag = observation.quality_flag,
    staged_observation.raw_context = observation.raw_context
MERGE (stage)-[:STAGED_OBSERVATION]->(staged_observation)
WITH sr, stage
OPTIONAL MATCH (stage)-[:STAGED_CONTAINS]->(line:StagedSalesLine)
WITH sr, stage, count(DISTINCT line) AS line_count
OPTIONAL MATCH (stage)-[:STAGED_OBSERVATION]->(observation:StagedSalesVehicleObservation)
WITH sr, stage, line_count, count(DISTINCT observation) AS observation_count
WHERE line_count = stage.expected_line_count
  AND observation_count = stage.expected_observation_count
SET sr.staged_sales_ready = true,
    sr.staged_sales_hash = $stage_hash,
    sr.staged_sales_line_count = stage.expected_line_count,
    sr.staged_sales_observation_count = stage.expected_observation_count
RETURN stage.source_record_pk AS source_record_pk
"""

#: Idempotent create of a Product. Matches on (source_system_key, source_product_id).
MERGE_PRODUCT = """
MERGE (p:Product {
    source_system_key: $source_system_key,
    source_product_id: $source_product_id
})
ON CREATE SET
    p.product_id   = randomUUID(),
    p.first_seen_at = datetime(),
    p.created_at   = datetime()
SET
    p.sku          = $sku,
    p.name         = $name,
    p.display_name = $display_name,
    p.category     = $category,
    p.subcategory  = $subcategory,
    p.manufacturer = $manufacturer,
    p.attributes   = $attributes,
    p.is_active    = coalesce($is_active, true),
    p.last_seen_at = datetime(),
    p.updated_at   = datetime()
RETURN p.product_id AS product_id
"""

#: Attach a Product to its owning Entity (entity-scoped catalogue).
LINK_PRODUCT_TO_ENTITY = """
MATCH (p:Product {source_system_key: $source_system_key, source_product_id: $source_product_id})
MATCH (e:Entity  {entity_key: $entity_key})
MERGE (p)-[:SOLD_BY]->(e)
"""

#: Idempotent create of an Order plus its SOLD_THROUGH edge to the booking
#: SourceSystem. Combining them saves a round-trip per sale (33k+ per full
#: run of SZ+Eko+Fundbox).
MERGE_ORDER = """
MATCH (ss:SourceSystem {source_key: $source_system_key})
MERGE (o:Order {
    source_system_key: $source_system_key,
    source_order_id:   $source_order_id
})
ON CREATE SET
    o.order_id   = randomUUID(),
    o.created_at = datetime()
SET
    o.order_no     = $order_no,
    o.ordered_at   = $ordered_at,
    o.release_date = $release_date,
    o.status       = $status,
    o.total_amount = $total_amount,
    o.currency     = $currency,
    o.item_count   = $item_count,
    o.metadata     = $metadata,
    o.points_used          = $points_used,
    o.points_gained        = $points_gained,
    o.did_redeem_discount  = $did_redeem_discount,
    o.is_purchase_points   = $is_purchase_points,
    o.non_vehicle_lines    = $non_vehicle_lines,
    o.updated_at   = datetime()
MERGE (o)-[:SOLD_THROUGH]->(ss)
RETURN o.order_id AS order_id
"""

#: Idempotent create of a LineItem and its attachment to its Order and Product.
MERGE_LINE_ITEM = """
MERGE (li:LineItem {
    source_system_key:   $source_system_key,
    source_line_item_id: $source_line_item_id
})
ON CREATE SET
    li.line_item_id = randomUUID(),
    li.created_at   = datetime()
SET
    li.line_no         = $line_no,
    li.quantity        = $quantity,
    li.unit_price      = $unit_price,
    li.line_total      = $line_total,
    li.currency        = $currency,
    li.discount_amount = $discount_amount,
    li.tax_amount      = $tax_amount,
    li.metadata        = $metadata
WITH li
OPTIONAL MATCH (:Order)-[prior:CONTAINS]->(li)
DELETE prior
WITH DISTINCT li
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
MERGE (o)-[:CONTAINS]->(li)
WITH li
OPTIONAL MATCH (li)-[prior_product:OF_PRODUCT]->(:Product)
DELETE prior_product
WITH DISTINCT li
MATCH (p:Product {source_system_key: $source_system_key, source_product_id: $source_product_id})
MERGE (li)-[:OF_PRODUCT]->(p)
RETURN li.line_item_id AS line_item_id
"""

#: Resolve a sales SourceRecord's linked customer SourceRecord to a Person,
#: via the pending ``FOR_CUSTOMER_RECORD`` edge. Returns the person_id when
#: the customer side has been resolved and linked.
RESOLVE_SALES_CUSTOMER = """
MATCH (sales_sr:SourceRecord {source_record_pk: $sales_source_record_pk})
      -[:FOR_CUSTOMER_RECORD]->(identity_sr:SourceRecord)
WHERE identity_sr.lifecycle_status = 'active'
   OR (
       identity_sr.lifecycle_status IS NULL
       AND identity_sr.is_latest = true
   )
WITH collect(DISTINCT identity_sr) AS identity_records
WHERE size(identity_records) = 1
UNWIND identity_records AS identity_sr
MATCH (identity_sr)-[:LINKED_TO]->(p:Person {status: 'active'})
WITH collect(DISTINCT p) AS persons
WHERE size(persons) = 1
UNWIND persons AS p
RETURN p.person_id AS person_id
"""

#: Link a customer SourceRecord to the existing identity SourceRecord it
#: references. Used when the sales record is first written; if the identity
#: record hasn't been ingested yet, the MATCH fails and the edge is not
#: created — the sales record stays in ``link_status='pending_customer'``.
#:
#: ``source_system_key`` is not a property on SourceRecord — it lives on
#: the FROM_SOURCE edge to SourceSystem, so we traverse it explicitly.
LINK_SALES_TO_IDENTITY_RECORD = """
MATCH (sales_sr:SourceRecord {source_record_pk: $sales_source_record_pk})
MATCH (identity_sr:SourceRecord {source_record_id: $identity_source_record_id})
      -[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system_key})
WHERE identity_sr.lifecycle_status = 'active'
   OR (
       identity_sr.lifecycle_status IS NULL
       AND identity_sr.is_latest = true
   )
WITH sales_sr, collect(DISTINCT identity_sr) AS identity_records
WHERE size(identity_records) = 1
UNWIND identity_records AS identity_sr
OPTIONAL MATCH (sales_sr)-[stale:FOR_CUSTOMER_RECORD]->(stale_identity:SourceRecord)
WHERE stale_identity <> identity_sr
DELETE stale
WITH DISTINCT sales_sr, identity_sr
MERGE (sales_sr)-[:FOR_CUSTOMER_RECORD]->(identity_sr)
RETURN identity_sr.source_record_pk AS identity_source_record_pk
"""

REPLACE_ORDER_LINES = """
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
CALL {
    WITH o
    OPTIONAL MATCH (o)-[contains:CONTAINS]->(stale:LineItem)
    WHERE NOT stale.source_line_item_id IN $source_line_item_ids
    OPTIONAL MATCH (stale)-[stale_product:OF_PRODUCT]->(:Product)
    DELETE contains, stale_product
    RETURN count(contains) AS removed_contains
}
WITH DISTINCT o, removed_contains
CALL {
    WITH o
    OPTIONAL MATCH (o)-[:CONTAINS]->(current:LineItem)-[old_product:OF_PRODUCT]->(:Product)
    WHERE current.source_line_item_id IN $source_line_item_ids
    DELETE old_product
    RETURN count(old_product) AS removed_product_links
}
RETURN removed_contains, removed_product_links
"""

#: Attach a resolved Person to the Order via PURCHASED. Deduplicated on
#: (source_system_key, source_order_id) so changed SourceRecord versions refresh
#: the same durable purchase edge instead of duplicating it.
LINK_PERSON_PURCHASED_ORDER = """
MATCH (person:Person {person_id: $person_id})
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
MERGE (person)-[rel:PURCHASED {
    source_system_key: $source_system_key,
    source_order_id:   $source_order_id
}]->(o)
ON CREATE SET
    rel.first_seen_at     = datetime(),
    rel.created_at        = datetime()
SET rel.source_record_pk  = $source_record_pk,
    rel.last_seen_at      = datetime(),
    rel.last_confirmed_at = datetime()
"""

CLEAR_SUPERSEDED_SALES_LINKS = """
CALL {
    OPTIONAL MATCH (:Person)-[purchase:PURCHASED {source_system_key: $source_system_key}]
                   ->(:Order)
    WHERE purchase.source_record_pk = $old_source_record_pk
    DELETE purchase
    RETURN count(purchase) AS retired_purchases
}
CALL {
    OPTIONAL MATCH (:Order {source_system_key: $source_system_key})
                   -[unit_rel:INVOLVES_VEHICLE]->(:Vehicle)
    WHERE unit_rel.source_record_pk = $old_source_record_pk
    DELETE unit_rel
    RETURN count(unit_rel) AS retired_order_vehicles
}
CALL {
    OPTIONAL MATCH (:Person)-[bought:BOUGHT_VEHICLE {source_system_key: $source_system_key}]
                   ->(:Vehicle)
    WHERE bought.source_record_pk = $old_source_record_pk
    DELETE bought
    RETURN count(bought) AS retired_bought_vehicles
}
RETURN retired_purchases, retired_order_vehicles, retired_bought_vehicles
"""

#: Find sales SourceRecords that are still waiting for their customer
#: identity record to be resolved. Scanned at end-of-run to drain the
#: pending-customer park queue.
#:
#: ``source_system_key`` lives on the ``FROM_SOURCE`` edge to ``SourceSystem``,
#: not on the SourceRecord itself; traverse to get the right value back to the
#: Python drain code.
FIND_PENDING_CUSTOMER_SALES = """
MATCH (sr:SourceRecord {record_type: 'sales', link_status: 'pending_customer'})
      -[:FROM_SOURCE]->(ss:SourceSystem)
WHERE sr.lifecycle_status = 'pending_review'
  AND sr.source_record_pk > $cursor
RETURN sr.source_record_pk AS source_record_pk,
       sr.source_record_id AS source_record_id,
       sr.expected_active_source_record_pk AS expected_active_source_record_pk,
       ss.source_key       AS source_system_key,
       sr.raw_payload      AS raw_payload
ORDER BY sr.source_record_pk
LIMIT $limit
"""

#: Mark a sales SourceRecord as linked once PURCHASED is in place.
MARK_SALES_RECORD_LINKED = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
SET sr.link_status = 'linked',
    sr.updated_at  = datetime()
"""

#: Mark a sales SourceRecord as permanently link-failed (a malformed customer_link,
#: an undecodable raw_payload, a missing FROM_SOURCE edge, or a corrupt resolved
#: Person). Transitioning to a terminal status stops FIND_PENDING_CUSTOMER_SALES
#: (which filters ``link_status='pending_customer'``) from re-scanning the row on
#: every drain tick, so the skip warning fires exactly once and the pending park
#: queue only retries records that can still plausibly link (the transient
#: "identity Person not yet resolved" path stays ``pending_customer`` and retries).
MARK_SALES_RECORD_LINK_FAILED = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
SET sr.link_status       = 'link_failed',
    sr.link_failed_reason = $reason,
    sr.link_failed_at     = datetime(),
    sr.updated_at         = datetime()
"""

#: Rewire PURCHASED edges when ``absorbed`` is merged into ``survivor``.
#: Mirrors the rewire pattern for IDENTIFIED_BY / LIVES_AT / KNOWS.
REWIRE_PURCHASED = """
MATCH (absorbed:Person {person_id: $absorbed_id})-[old:PURCHASED]->(o:Order)
WITH old, o, properties(old) AS props
DELETE old
WITH o, props
MATCH (survivor:Person {person_id: $survivor_id})
MERGE (survivor)-[rel:PURCHASED {
    source_system_key: props.source_system_key,
    source_record_pk:  props.source_record_pk
}]->(o)
ON CREATE SET rel += props
RETURN count(o) AS rewired_count
"""

#: Find active Person candidates who share a Vehicle with a pending-customer
#: sales SourceRecord (via the INVOLVES_VEHICLE edges already written by the
#: sales pipeline). Per the SDD task 2 brief, the minimum match is "vehicle
#: identity AND (mobile OR email)" — so the contact-channel overlap is a
#: REQUIRED filter, not optional metadata. Only persons who BOTH share a
#: Vehicle identity with the sale AND have an email or phone identifier
#: matching the sale's customer are returned. ``contact_channels`` records
#: which channels matched (for the heuristic's scoring/feature_snapshot).
#: ``nric_blocked`` flags candidates whose NRIC differs from the sale's
#: ``customer_nric``. Used by propose_vehicle_matches_for_pending_sales.
#:
#: A pair (sale, person) that already has a ``MatchDecision`` (MATCH, REVIEW,
#: or NO_MATCH — including the NRIC-block NO_MATCH recorded by
#: ``_propose_one_pending_sale``) is excluded from re-proposal: once a decision
#: exists for a ``(SourceRecord, Person)`` pair it is not re-proposed. This
#: prevents the NRIC-block path from re-recording a duplicate NO_MATCH on every
#: run. The filter mirrors ``persist_match_decision``'s wiring: the decision is
#: linked to the sale's ``SourceRecord`` via ``ABOUT_LEFT {entity_type:
#: 'source_record'}`` and to the candidate ``Person`` via ``ABOUT_RIGHT
#: {entity_type: 'person'}``.
#:
#: If a sale's only candidate is NRIC-blocked, the exclusion returns zero
#: candidates on the next run → ``_propose_one_pending_sale`` returns False and
#: the sale stays ``pending_customer`` (no duplicate decision is written). That
#: is acceptable: the sale is effectively terminal for the vehicle path; it can
#: still be drained later if the customer identity resolves through another
#: source and the MatchDecision is superseded.
#:
#: If a sale has NO customer emails and NO customer phones (both lists empty),
#: the required MATCH on ``(pi:Identifier)`` yields zero rows and the query
#: returns no candidates — vehicle identity alone is not enough.
FIND_VEHICLE_CANDIDATES_FOR_SALES = """
MATCH (sr:SourceRecord {source_record_pk: $sales_source_record_pk, link_status: 'pending_customer'})
// Candidate identity is derived from staged raw payload, never a visible
// INVOLVES_VEHICLE {source_record_pk: $sales_source_record_pk} projection.
MATCH (v:Vehicle)
WHERE v.normalized_serial_number IN $normalized_serial_numbers
   OR v.normalized_lta_tag IN $normalized_lta_tags
MATCH (v)<-[rel:BOUGHT_VEHICLE|OWNS_VEHICLE]-(p:Person {status: 'active'})
WHERE NOT EXISTS {
    MATCH (md:MatchDecision)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(sr)
    MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(p)
}
MATCH (pi:Identifier)
WHERE ((pi.value IN $customer_emails AND pi.kind IN ['email'])
   OR (pi.value IN $customer_phones AND pi.kind IN ['mobile','phone']))
  AND (p)-[:IDENTIFIED_BY]->(pi)
WITH sr, v, p, rel, collect(DISTINCT pi.kind) AS contact_channels, $customer_nric AS customer_nric
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(ni:Identifier)
WHERE ni.kind IN ['nric','nric_hash'] AND customer_nric IS NOT NULL AND customer_nric <> '' AND ni.value <> customer_nric
WITH sr, v, p, rel, contact_channels, collect(DISTINCT ni.value) AS mismatched_nrics
RETURN p.person_id AS person_id,
       v.vehicle_id AS vehicle_id,
       type(rel) AS rel_type,
       rel.is_active AS is_active,
       v.conflict_flag AS conflict_flag,
       rel.last_confirmed_at AS last_confirmed_at,
       contact_channels,
       size(mismatched_nrics) > 0 AS nric_blocked
ORDER BY rel_type, rel.is_active DESC, rel.last_confirmed_at DESC
"""

#: Transition a pending-customer sales SourceRecord to pending_review once a
#: vehicle-based MatchDecision + ReviewCase have been created for it.
MARK_SALES_RECORD_PENDING_REVIEW = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
SET sr.link_status = 'pending_review',
    sr.updated_at  = datetime()
"""
