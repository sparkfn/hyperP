"""Cypher constants for Vehicle nodes and relationships.

A ``Vehicle`` is the cross-source identity record for a physical unit (a
motorcycle / scooter / VIN-bearing product). Identity is anchored globally by
``normalized_lta_tag`` (the Land Transport Authority plate) with a per-source
fallback on ``(source_system_key, product_sku, normalized_serial_number)``.
``UPSERT_VEHICLE`` is the cross-source upsert; the remaining constants mirror
the previous unit-based write surfaces, swapping rel types to
``INVOLVES_VEHICLE``/``BOUGHT_VEHICLE``/``OWNS_VEHICLE``/``MENTIONS_VEHICLE``.

Query modules disable ``E501`` (see ``pyproject.toml`` overrides) so the Cypher
is not artificially wrapped.
"""

from __future__ import annotations

# Cross-source Vehicle upsert.
#
# Identity resolution rules (see SDD task 2 brief):
#   1. LTA match is GLOBAL: any Vehicle whose ``normalized_lta_tag`` equals the
#      caller's, regardless of source-system. ``vehicle_lta_unique`` backs this.
#   2. Serial match is PER-SOURCE: a Vehicle only matches on serial when the
#      caller's ``source_system_key`` is already in ``v.source_systems`` AND
#      the serial matches AND the SKU is in ``observed_product_skus_s`` (or
#      equals the first-observed ``product_sku``). There is no unique constraint
#      on serial, so two different sources can carry the same serial as two
#      distinct Vehicles (test case 2).
#   3. Conflict: LTA matches one Vehicle AND serial matches a *different*
#      Vehicle. Both are flagged ``identifier_conflict`` and the caller's
#      identifying fields are NOT written onto either (no merge). The target is
#      the LTA match; we only stamp ``updated_at``/``conflict_flag`` on it.
#   4. Promotion: a serial-only Vehicle that later gains an LTA resolves to
#      itself (``ser_match`` with ``lta_match`` null) and gets its
#      ``normalized_lta_tag`` filled in-place (test case 3) -- no new Vehicle.
#
# The create-or-match uses a ``CALL { ... UNION ... }`` so the outer row is
# preserved whether or not a target already exists (a plain ``FOREACH`` CREATE
# would leave the created node unbound afterwards). The fill SET runs only when
# ``NOT id_conflict`` so identifying fields are never polluted in the conflict
# state.
UPSERT_VEHICLE = """
OPTIONAL MATCH (lta_match:Vehicle)
  WHERE $normalized_lta_tag IS NOT NULL
    AND lta_match.normalized_lta_tag = $normalized_lta_tag
OPTIONAL MATCH (ser_match:Vehicle)
  WHERE $normalized_serial_number IS NOT NULL
    AND $source_system_key IN ser_match.source_systems
    AND ser_match.normalized_serial_number = $normalized_serial_number
    AND ($product_sku IN coalesce(ser_match.observed_product_skus_s, []) OR ser_match.product_sku = $product_sku)
WITH lta_match, ser_match,
     CASE WHEN lta_match IS NOT NULL AND ser_match IS NOT NULL AND lta_match <> ser_match
          THEN true ELSE false END AS id_conflict
FOREACH (_ IN CASE WHEN id_conflict THEN [1] ELSE [] END |
  SET lta_match.conflict_flag = true,
      lta_match.conflict_reason = 'identifier_conflict',
      lta_match.updated_at = $observed_at,
      ser_match.conflict_flag = true,
      ser_match.conflict_reason = 'identifier_conflict',
      ser_match.updated_at = $observed_at
)
WITH lta_match, ser_match, id_conflict
CALL {
  WITH lta_match, ser_match
  WITH coalesce(lta_match, ser_match) AS target
  WHERE target IS NOT NULL
  RETURN target AS v
  UNION
  WITH lta_match, ser_match
  WITH coalesce(lta_match, ser_match) AS target
  WHERE target IS NULL
  CREATE (v:Vehicle {vehicle_id: randomUUID(), created_at: $observed_at})
  RETURN v
}
WITH v, id_conflict
FOREACH (_ IN CASE WHEN NOT id_conflict THEN [1] ELSE [] END |
  SET v.normalized_lta_tag = coalesce(v.normalized_lta_tag, $normalized_lta_tag),
      v.normalized_serial_number = coalesce(v.normalized_serial_number, $normalized_serial_number),
      v.lta_tag = coalesce(v.lta_tag, $lta_tag),
      v.serial_number = coalesce(v.serial_number, $serial_number),
      v.product_sku = coalesce(v.product_sku, $product_sku),
      v.product = coalesce(v.product, $product),
      v.manufacturer = coalesce(v.manufacturer, $manufacturer),
      v.model = coalesce(v.model, $model),
      v.source_systems = CASE WHEN $source_system_key IN coalesce(v.source_systems, [])
                               THEN v.source_systems
                               ELSE coalesce(v.source_systems, []) + [$source_system_key] END,
      v.observed_product_skus_s = CASE WHEN $product_sku IS NULL OR $product_sku IN coalesce(v.observed_product_skus_s, [])
                                        THEN v.observed_product_skus_s
                                        ELSE coalesce(v.observed_product_skus_s, []) + [$product_sku] END
)
SET v.updated_at = $observed_at,
    v.conflict_flag = coalesce(v.conflict_flag, id_conflict),
    v.conflict_reason = CASE WHEN id_conflict THEN 'identifier_conflict' ELSE v.conflict_reason END
RETURN v.vehicle_id AS vehicle_id, coalesce(v.conflict_flag, false) AS conflict
"""

# Read-only Vehicle resolution for chat ingestion. Chat never creates a
# Vehicle; it only links a conversation SourceRecord to *already-known*
# Vehicles. Identity is resolved by:
#   1. GLOBAL LTA match: any Vehicle whose ``normalized_lta_tag`` equals the
#      caller's (``vehicle_lta_unique`` backs this). Chat does not carry a
#      source-system context, so LTA is matched across all sources.
#   2. Serial + product-NAME match: a Vehicle matches on serial when the serial
#      matches AND its product NAME matches the chat inquiry's product name
#      (case-insensitive, trimmed). Chat inquiries carry a free-text product
#      NAME from LLM extraction, not a source-internal SKU, and the chat source
#      is not the sales source, so the previous ``source_system_key IN
#      v.source_systems AND product_sku`` predicate never matched. Product-name
#      match is the cross-source bridge.
# Both branches are OR-ed in a single ``MATCH`` so LTA and serial matches
# de-duplicate naturally. The caller links ``MENTIONS_VEHICLE`` only when
# exactly one Vehicle matched.
#
# Identity rule (spec §3 "merges" — LTA-only Vehicles cross sources):
#   The serial+product branch is a *fallback* that only fires when the caller
#   also carries an LTA tag. LTA-tagged Vehicles are the only ones that may
#   cross sources via chat; serial+product alone matches in-source identity
#   exactly and is not used as a cross-source merge key.
RESOLVE_EXISTING_VEHICLE_FOR_CHAT = """
MATCH (v:Vehicle)
WHERE (
    $normalized_lta_tag IS NOT NULL
    AND v.normalized_lta_tag = $normalized_lta_tag
  )
  OR (
    $normalized_lta_tag IS NOT NULL
    AND $normalized_serial_number IS NOT NULL
    AND $product IS NOT NULL
    AND v.normalized_lta_tag = $normalized_lta_tag
    AND v.normalized_serial_number = $normalized_serial_number
    AND v.product IS NOT NULL
    AND toLower(trim(v.product)) = toLower(trim($product))
  )
RETURN collect(DISTINCT v.vehicle_id) AS vehicle_ids
"""

# Link a conversation SourceRecord to a single already-resolved Vehicle. The
# caller (chat pipeline) only invokes this when exactly one Vehicle matched.
LINK_CHAT_SOURCE_RECORD_MENTIONS_VEHICLE = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (sr)-[rel:MENTIONS_VEHICLE]->(v)
SET rel.source_system_key = $source_system_key,
    rel.source_record_id = $source_record_id,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.updated_at = datetime()
"""

# Attach an Order to one of its Vehicles (the sales sub-graph edge written
# during sales ingestion, one per line that carries a vehicle reference).
LINK_ORDER_INVOLVES_VEHICLE = """
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (o)-[rel:INVOLVES_VEHICLE {
    source_system_key: $source_system_key,
    source_record_pk: $source_record_pk
}]->(v)
ON CREATE SET rel.created_at = datetime()
SET rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.updated_at = datetime()
"""

# Record that a Person bought a Vehicle on a given order. The MERGE key is
# ``(source_system_key, source_order_id)`` so re-ingesting the same order is
# idempotent; ``is_active`` toggles ownership currency without dropping history.
LINK_PERSON_BOUGHT_VEHICLE = """
MATCH (p:Person {person_id: $person_id})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (p)-[rel:BOUGHT_VEHICLE {
    source_system_key: $source_system_key,
    source_order_id:   $source_order_id
}]->(v)
ON CREATE SET rel.created_at = datetime(),
              rel.first_seen_at = datetime()
SET rel.source_record_pk = $source_record_pk,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.is_active = $is_active,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at = datetime()
"""

# Generic "this SourceRecord mentions this Vehicle" edge, used by non-chat
# ingestion paths that observe a Vehicle reference inside a record.
LINK_SOURCE_RECORD_MENTIONS_VEHICLE = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (sr)-[rel:MENTIONS_VEHICLE]->(v)
SET rel.source_system_key = $source_system_key,
    rel.source_record_id = $source_record_id,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.updated_at = datetime()
"""

# Explicit ownership edge (registration / LTA record, not a sale). Idempotent on
# ``(source_system_key, source_record_pk)``; ``is_active`` and
# ``last_confirmed_at`` carry the currency of the ownership claim.
LINK_PERSON_OWNS_VEHICLE = """
MATCH (p:Person {person_id: $person_id})
MATCH (v:Vehicle {vehicle_id: $vehicle_id})
MERGE (p)-[rel:OWNS_VEHICLE {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk
}]->(v)
ON CREATE SET rel.created_at = datetime(),
              rel.first_seen_at = datetime(),
              rel.is_active = true
SET rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at = datetime()
"""

# Flag Vehicles owned by more than one active Person. Mirrors the legacy
# unit-owner conflict check: collects distinct owner person_ids, flags the
# Vehicle and each owning edge when >1 active owner is present.
FLAG_VEHICLE_OWNER_CONFLICTS = """
MATCH (v:Vehicle)<-[rel:OWNS_VEHICLE {is_active: true}]-(p:Person {status: 'active'})
WITH v, collect(DISTINCT p.person_id) AS owner_ids, collect(rel) AS rels
WHERE size(owner_ids) > 1
SET v.conflict_flag = true,
    v.conflict_reason = 'multiple_active_owners',
    v.updated_at = datetime()
FOREACH (rel IN rels | SET rel.conflict_flag = true, rel.updated_at = datetime())
RETURN v.vehicle_id AS vehicle_id, owner_ids AS owner_ids
"""
