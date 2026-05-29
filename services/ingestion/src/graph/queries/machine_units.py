"""Cypher constants for MachineUnit nodes and relationships."""

from __future__ import annotations

UPSERT_MACHINE_UNIT = """
OPTIONAL MATCH (lta_unit:MachineUnit {
    normalized_machine_product: $normalized_machine_product,
    normalized_lta_tag: $normalized_lta_tag
})
  WHERE $normalized_machine_product IS NOT NULL AND $normalized_lta_tag IS NOT NULL
OPTIONAL MATCH (serial_unit:MachineUnit {
    normalized_machine_product: $normalized_machine_product,
    normalized_serial_number: $normalized_serial_number
})
  WHERE $normalized_machine_product IS NOT NULL AND $normalized_serial_number IS NOT NULL
WITH lta_unit, serial_unit
CALL {
  WITH lta_unit, serial_unit
  WITH lta_unit, serial_unit
  WHERE lta_unit IS NOT NULL AND serial_unit IS NOT NULL AND lta_unit <> serial_unit
  SET lta_unit.conflict_flag = true,
      serial_unit.conflict_flag = true,
      lta_unit.conflict_reason = 'machine_unit_identifier_conflict',
      serial_unit.conflict_reason = 'machine_unit_identifier_conflict',
      lta_unit.machine_product = coalesce(lta_unit.machine_product, $machine_product),
      serial_unit.machine_product = coalesce(serial_unit.machine_product, $machine_product),
      lta_unit.normalized_machine_product = coalesce(lta_unit.normalized_machine_product, $normalized_machine_product),
      serial_unit.normalized_machine_product = coalesce(serial_unit.normalized_machine_product, $normalized_machine_product),
      lta_unit.updated_at = datetime(),
      serial_unit.updated_at = datetime()
  RETURN lta_unit AS unit, true AS conflict
  UNION
  WITH lta_unit, serial_unit
  WITH lta_unit, serial_unit, coalesce(lta_unit, serial_unit) AS matched
  WHERE matched IS NOT NULL AND (lta_unit IS NULL OR serial_unit IS NULL OR lta_unit = serial_unit)
  SET matched.machine_product = coalesce(matched.machine_product, $machine_product),
      matched.normalized_machine_product = coalesce(matched.normalized_machine_product, $normalized_machine_product),
      matched.lta_tag = coalesce(matched.lta_tag, $lta_tag),
      matched.normalized_lta_tag = coalesce(matched.normalized_lta_tag, $normalized_lta_tag),
      matched.serial_number = coalesce(matched.serial_number, $serial_number),
      matched.normalized_serial_number = coalesce(matched.normalized_serial_number, $normalized_serial_number),
      matched.updated_at = datetime()
  RETURN matched AS unit, false AS conflict
  UNION
  WITH lta_unit, serial_unit
  WITH lta_unit, serial_unit
  WHERE lta_unit IS NULL AND serial_unit IS NULL
  CREATE (created:MachineUnit {
      machine_unit_id: randomUUID(),
      machine_product: $machine_product,
      normalized_machine_product: $normalized_machine_product,
      lta_tag: $lta_tag,
      normalized_lta_tag: $normalized_lta_tag,
      serial_number: $serial_number,
      normalized_serial_number: $normalized_serial_number,
      conflict_flag: false,
      created_at: datetime(),
      updated_at: datetime()
  })
  RETURN created AS unit, false AS conflict
}
RETURN unit.machine_unit_id AS machine_unit_id,
       conflict AS conflict
LIMIT 1
"""

RESOLVE_EXISTING_MACHINE_UNIT_FOR_CHAT = """
MATCH (u:MachineUnit)
WHERE (
    $normalized_lta_tag IS NOT NULL
    AND u.normalized_lta_tag = $normalized_lta_tag
    AND ($normalized_machine_product IS NULL OR u.normalized_machine_product = $normalized_machine_product)
  )
  OR (
    $normalized_serial_number IS NOT NULL
    AND u.normalized_serial_number = $normalized_serial_number
    AND ($normalized_machine_product IS NULL OR u.normalized_machine_product = $normalized_machine_product)
  )
RETURN collect(DISTINCT u.machine_unit_id) AS machine_unit_ids
"""

LINK_CHAT_SOURCE_RECORD_MENTIONS_EXISTING_UNIT = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (sr)-[rel:MENTIONS_UNIT]->(u)
SET rel.source_system_key = $source_system_key,
    rel.source_record_id = $source_record_id,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.updated_at = datetime()
"""

LINK_ORDER_INVOLVES_UNIT = """
MATCH (o:Order {source_system_key: $source_system_key, source_order_id: $source_order_id})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (o)-[rel:INVOLVES_UNIT {
    source_system_key: $source_system_key
}]->(u)
ON CREATE SET rel.created_at = datetime()
SET rel.source_record_pk = $source_record_pk,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.updated_at = datetime()
"""

LINK_PERSON_BOUGHT_UNIT = """
MATCH (p:Person {person_id: $person_id})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (p)-[rel:BOUGHT_UNIT {
    source_system_key: $source_system_key,
    source_order_id:   $source_order_id
}]->(u)
ON CREATE SET rel.created_at = datetime(),
              rel.first_seen_at = datetime()
SET rel.source_record_pk = $source_record_pk,
    rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.last_seen_at = datetime(),
    rel.last_confirmed_at = datetime(),
    rel.updated_at = datetime()
"""

LINK_SOURCE_RECORD_MENTIONS_UNIT = """
MATCH (sr:SourceRecord {source_record_pk: $source_record_pk})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (sr)-[rel:MENTIONS_UNIT]->(u)
ON CREATE SET rel.created_at = datetime()
SET rel.raw_context = $raw_context,
    rel.observed_at = $observed_at,
    rel.confidence = $confidence,
    rel.quality_flag = $quality_flag,
    rel.updated_at = datetime()
"""

LINK_PERSON_OWNS_UNIT = """
MATCH (p:Person {person_id: $person_id})
MATCH (u:MachineUnit {machine_unit_id: $machine_unit_id})
MERGE (p)-[rel:OWNS_UNIT {
    source_system_key: $source_system_key,
    source_record_pk:  $source_record_pk
}]->(u)
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

FLAG_MACHINE_UNIT_OWNER_CONFLICTS = """
MATCH (u:MachineUnit)<-[rel:OWNS_UNIT {is_active: true}]-(p:Person {status: 'active'})
WITH u, collect(DISTINCT p.person_id) AS owner_ids, collect(rel) AS rels
WHERE size(owner_ids) > 1
SET u.conflict_flag = true,
    u.conflict_reason = 'multiple_active_owners',
    u.updated_at = datetime()
FOREACH (rel IN rels | SET rel.conflict_flag = true, rel.updated_at = datetime())
RETURN u.machine_unit_id AS machine_unit_id, owner_ids AS owner_ids
"""
