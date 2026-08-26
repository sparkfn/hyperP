"""Cypher constants for manual merge / unmerge / pair-lock operations."""

from __future__ import annotations

CHECK_NO_MATCH_LOCK = """
MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
WHERE lock.lock_type = 'manual_no_match'
  AND (lock.expires_at IS NULL OR lock.expires_at > datetime())
RETURN count(lock) > 0 AS is_locked
"""

CHECK_BOTH_PERSONS_ACTIVE = """
MATCH (absorbed:Person {person_id: $from_id, status: 'active'})
MATCH (survivor:Person {person_id: $to_id, status: 'active'})
RETURN absorbed, survivor
"""

EXECUTE_MANUAL_MERGE = """
MATCH (lock_first:Person {person_id: $left})
MATCH (lock_second:Person {person_id: $right})
WHERE lock_first <> lock_second
SET lock_first.merge_lock_version = coalesce(lock_first.merge_lock_version, 0) + 1
SET lock_second.merge_lock_version = coalesce(lock_second.merge_lock_version, 0) + 1
WITH lock_first, lock_second
MATCH (absorbed:Person {person_id: $from_id, status: 'active'})
MATCH (survivor:Person {person_id: $to_id, status: 'active'})

CREATE (me:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'manual_merge',
  actor_type: 'admin',
  actor_id: $actor_id,
  reason: $reason,
  metadata: '{}',
  created_at: datetime()
})
CREATE (me)-[:ABSORBED]->(absorbed)
CREATE (me)-[:SURVIVOR]->(survivor)

WITH absorbed, survivor, me
OPTIONAL MATCH (sr:SourceRecord)-[old_link:LINKED_TO]->(absorbed)
WITH absorbed, survivor, me, sr, old_link, properties(old_link) AS old_link_props
FOREACH (_ IN CASE WHEN old_link IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(sr)
  SET moved = old_link_props,
      moved.relationship_type = 'LINKED_TO',
      moved.direction = 'incoming',
      moved.origin_person_id = coalesce(
        old_link_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = true
  CREATE (sr)-[new_link:LINKED_TO]->(survivor)
  SET new_link = old_link_props,
      new_link.linked_at = datetime(),
      new_link.merge_origin_person_id = moved.origin_person_id
  CREATE (me)-[:AFFECTED_RECORD]->(sr)
  DELETE old_link
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_id:IDENTIFIED_BY]->(id:Identifier)
OPTIONAL MATCH (id_sr:SourceRecord {source_record_pk: old_id.source_record_pk})
WITH absorbed, survivor, me, id, id_sr, old_id, properties(old_id) AS old_id_props
FOREACH (_ IN CASE WHEN old_id IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(id)
  SET moved = old_id_props,
      moved.relationship_type = 'IDENTIFIED_BY',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        old_id_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = true
  CREATE (survivor)-[new_id:IDENTIFIED_BY]->(id)
  SET new_id = old_id_props,
      new_id.merge_origin_person_id = moved.origin_person_id
  DELETE old_id
)
FOREACH (_ IN CASE WHEN id_sr IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(id_sr)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_addr:LIVES_AT]->(addr:Address)
OPTIONAL MATCH (addr_sr:SourceRecord {source_record_pk: old_addr.source_record_pk})
WITH absorbed, survivor, me, addr, addr_sr, old_addr, properties(old_addr) AS old_addr_props
FOREACH (_ IN CASE WHEN old_addr IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(addr)
  SET moved = old_addr_props,
      moved.relationship_type = 'LIVES_AT',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        old_addr_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = true
  CREATE (survivor)-[new_addr:LIVES_AT]->(addr)
  SET new_addr = old_addr_props,
      new_addr.merge_origin_person_id = moved.origin_person_id
  DELETE old_addr
)
FOREACH (_ IN CASE WHEN addr_sr IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(addr_sr)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_k_out:KNOWS]->(k_other:Person)
WHERE k_other.person_id <> survivor.person_id
OPTIONAL MATCH (knows_source:SourceRecord {source_record_pk: old_k_out.source_record_pk})
WITH absorbed, survivor, me, k_other, knows_source, old_k_out,
     properties(old_k_out) AS old_k_out_props
FOREACH (_ IN CASE WHEN old_k_out IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(k_other)
  SET moved = old_k_out_props,
      moved.relationship_type = 'KNOWS_OUT',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        old_k_out_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = true
  CREATE (survivor)-[new_k_out:KNOWS]->(k_other)
  SET new_k_out = old_k_out_props
  SET new_k_out.declared_by_person_id = survivor.person_id,
      new_k_out.updated_at = datetime(),
      new_k_out.merge_origin_person_id = moved.origin_person_id
  DELETE old_k_out
)
FOREACH (_ IN CASE WHEN knows_source IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(knows_source)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (k_other2:Person)-[old_k_in:KNOWS]->(absorbed)
WHERE k_other2.person_id <> survivor.person_id
OPTIONAL MATCH (knows_source:SourceRecord {source_record_pk: old_k_in.source_record_pk})
WITH absorbed, survivor, me, k_other2, knows_source, old_k_in,
     properties(old_k_in) AS old_k_in_props
FOREACH (_ IN CASE WHEN old_k_in IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(k_other2)
  SET moved = old_k_in_props,
      moved.relationship_type = 'KNOWS_IN',
      moved.direction = 'incoming',
      moved.origin_person_id = coalesce(
        old_k_in_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = true
  CREATE (k_other2)-[new_k_in:KNOWS]->(survivor)
  SET new_k_in = old_k_in_props
  SET new_k_in.updated_at = datetime(),
      new_k_in.merge_origin_person_id = moved.origin_person_id
  DELETE old_k_in
)
FOREACH (_ IN CASE WHEN knows_source IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(knows_source)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_fact:HAS_FACT]->(sr_fact:SourceRecord)
WITH absorbed, survivor, me, sr_fact, old_fact, properties(old_fact) AS old_fact_props
FOREACH (_ IN CASE WHEN old_fact IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(sr_fact)
  SET moved = old_fact_props,
      moved.relationship_type = 'HAS_FACT',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        old_fact_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = true
  CREATE (survivor)-[new_fact:HAS_FACT]->(sr_fact)
  SET new_fact = old_fact_props,
      new_fact.merge_origin_person_id = moved.origin_person_id
  DELETE old_fact
)
FOREACH (_ IN CASE WHEN sr_fact IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(sr_fact)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_context:PURCHASED]->(context_order:Order)
OPTIONAL MATCH (context_source:SourceRecord {source_record_pk: old_context.source_record_pk})
OPTIONAL MATCH (survivor)-[existing_context:PURCHASED {
  source_system_key: old_context.source_system_key,
  source_order_id: old_context.source_order_id
}]->(context_order)
WITH absorbed, survivor, me, context_order, context_source, existing_context, old_context,
     properties(old_context) AS context_props
FOREACH (_ IN CASE WHEN old_context IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(context_order)
  SET moved = context_props,
      moved.relationship_type = 'PURCHASED',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        context_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = existing_context IS NULL
  MERGE (survivor)-[new_context:PURCHASED {
    source_system_key: old_context.source_system_key,
    source_order_id: old_context.source_order_id
  }]->(context_order)
  ON CREATE SET new_context += context_props,
                new_context.merge_origin_person_id = moved.origin_person_id
  DELETE old_context
)
FOREACH (_ IN CASE WHEN context_source IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(context_source)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_context:BOUGHT_VEHICLE]->(context_vehicle:Vehicle)
OPTIONAL MATCH (context_source:SourceRecord {source_record_pk: old_context.source_record_pk})
OPTIONAL MATCH (survivor)-[existing_context:BOUGHT_VEHICLE {
  source_system_key: old_context.source_system_key,
  source_order_id: old_context.source_order_id
}]->(context_vehicle)
WITH absorbed, survivor, me, context_vehicle, context_source, existing_context, old_context,
     properties(old_context) AS context_props
FOREACH (_ IN CASE WHEN old_context IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(context_vehicle)
  SET moved = context_props,
      moved.relationship_type = 'BOUGHT_VEHICLE',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        context_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = existing_context IS NULL
  MERGE (survivor)-[new_context:BOUGHT_VEHICLE {
    source_system_key: old_context.source_system_key,
    source_order_id: old_context.source_order_id
  }]->(context_vehicle)
  ON CREATE SET new_context += context_props,
                new_context.merge_origin_person_id = moved.origin_person_id
  DELETE old_context
)
FOREACH (_ IN CASE WHEN context_source IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(context_source)
)

WITH DISTINCT absorbed, survivor, me
OPTIONAL MATCH (absorbed)-[old_context:OWNS_VEHICLE]->(context_vehicle:Vehicle)
OPTIONAL MATCH (context_source:SourceRecord {source_record_pk: old_context.source_record_pk})
OPTIONAL MATCH (survivor)-[existing_context:OWNS_VEHICLE {
  source_system_key: old_context.source_system_key,
  source_record_pk: old_context.source_record_pk
}]->(context_vehicle)
WITH absorbed, survivor, me, context_vehicle, context_source, existing_context, old_context,
     properties(old_context) AS context_props
FOREACH (_ IN CASE WHEN old_context IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved:MOVED_RELATIONSHIP]->(context_vehicle)
  SET moved = context_props,
      moved.relationship_type = 'OWNS_VEHICLE',
      moved.direction = 'outgoing',
      moved.origin_person_id = coalesce(
        context_props.merge_origin_person_id, absorbed.person_id
      ),
      moved.created_on_survivor = existing_context IS NULL
  MERGE (survivor)-[new_context:OWNS_VEHICLE {
    source_system_key: old_context.source_system_key,
    source_record_pk: old_context.source_record_pk
  }]->(context_vehicle)
  ON CREATE SET new_context += context_props,
                new_context.merge_origin_person_id = moved.origin_person_id
  DELETE old_context
)
FOREACH (_ IN CASE WHEN context_source IS NOT NULL THEN [1] ELSE [] END |
  MERGE (me)-[:AFFECTED_RECORD]->(context_source)
)

WITH DISTINCT absorbed, survivor, me
SET absorbed.status = 'merged', absorbed.updated_at = datetime()
CREATE (absorbed)-[:MERGED_INTO {
  merge_event_id: me.merge_event_id,
  actor: $actor_id,
  timestamp: datetime()
}]->(survivor)

WITH absorbed, survivor, me
OPTIONAL MATCH (prev:Person)-[old_merge:MERGED_INTO]->(absorbed)
WITH absorbed, survivor, me, prev, old_merge, properties(old_merge) AS old_merge_props
FOREACH (_ IN CASE WHEN old_merge IS NOT NULL THEN [1] ELSE [] END |
  CREATE (me)-[moved_lineage:MOVED_MERGE_LINEAGE]->(prev)
  SET moved_lineage = old_merge_props,
      moved_lineage.prior_survivor_person_id = absorbed.person_id,
      moved_lineage.compressed_survivor_person_id = survivor.person_id
  DELETE old_merge
  CREATE (prev)-[compressed_merge:MERGED_INTO]->(survivor)
  SET compressed_merge = old_merge_props
)

WITH survivor, me
OPTIONAL MATCH (survivor)-[:KNOWS]-(merge_neighbor:Person {status: 'active'})
WITH survivor, me, collect(DISTINCT merge_neighbor) AS merge_neighbors
FOREACH (merge_neighbor IN merge_neighbors |
  SET merge_neighbor.analysis_input_revision =
        coalesce(merge_neighbor.analysis_input_revision, 0) + 1,
      merge_neighbor.analysis_dirty_at = datetime()
)
SET survivor.analysis_input_revision = coalesce(survivor.analysis_input_revision, 0) + 1,
    survivor.analysis_dirty_at = datetime(),
    survivor.updated_at = datetime()
RETURN me.merge_event_id AS merge_event_id, toString(me.created_at) AS created_at
"""

GET_UNMERGE_TARGET = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})
MATCH (me)-[:ABSORBED]->(absorbed:Person)
MATCH (me)-[:SURVIVOR]->(survivor:Person)
WHERE absorbed.status = 'merged'
RETURN absorbed.person_id AS absorbed_id, survivor.person_id AS survivor_id
"""

REVERT_MERGE = """
MATCH (absorbed:Person {person_id: $absorbed_id})
      -[mi:MERGED_INTO {merge_event_id: $merge_event_id}]->(merge_survivor:Person)
MATCH (merge_survivor)-[:MERGED_INTO*0..1]->(current_survivor:Person)
WHERE NOT (current_survivor)-[:MERGED_INTO]->(:Person)
MATCH (merge_event:MergeEvent {merge_event_id: mi.merge_event_id})
WITH absorbed, mi, merge_event, current_survivor,
     current_survivor.person_id AS current_survivor_id

OPTIONAL MATCH (merge_event)-[moved_lineage:MOVED_MERGE_LINEAGE]->(lineage_person:Person)
OPTIONAL MATCH (lineage_person)-[compressed_lineage:MERGED_INTO]->(current_survivor)
WHERE compressed_lineage.merge_event_id = moved_lineage.merge_event_id
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     moved_lineage, lineage_person, compressed_lineage,
     properties(moved_lineage) AS lineage_props
FOREACH (_ IN CASE WHEN moved_lineage IS NOT NULL
                         AND moved_lineage.prior_survivor_person_id = absorbed.person_id
                         AND lineage_person.status = 'merged'
                         AND compressed_lineage IS NOT NULL THEN [1] ELSE [] END |
  DELETE compressed_lineage
  CREATE (lineage_person)-[restored_lineage:MERGED_INTO]->(absorbed)
  SET restored_lineage = lineage_props,
      restored_lineage.prior_survivor_person_id = null,
      restored_lineage.compressed_survivor_person_id = null
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id

OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(sr:SourceRecord)
WHERE move.relationship_type = 'LINKED_TO'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     sr, move, origin, properties(move) AS move_props
OPTIONAL MATCH (sr)-[survivor_link:LINKED_TO]->(current_survivor)
WHERE survivor_link.merge_origin_person_id = move.origin_person_id
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  CREATE (sr)-[restored_link:LINKED_TO]->(absorbed)
  SET restored_link = move_props,
      restored_link.relationship_type = null,
      restored_link.direction = null,
      restored_link.origin_person_id = null,
      restored_link.created_on_survivor = null,
      restored_link.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_link IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_link
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(id:Identifier)
WHERE move.relationship_type = 'IDENTIFIED_BY'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     id, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_id:IDENTIFIED_BY {
  source_system_key: move.source_system_key,
  source_record_pk: move.source_record_pk
}]->(id)
WHERE survivor_id.merge_origin_person_id = move.origin_person_id
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  MERGE (absorbed)-[restored_id:IDENTIFIED_BY {
    source_system_key: move.source_system_key,
    source_record_pk: move.source_record_pk
  }]->(id)
  SET restored_id = move_props,
      restored_id.relationship_type = null,
      restored_id.direction = null,
      restored_id.origin_person_id = null,
      restored_id.created_on_survivor = null,
      restored_id.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_id IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_id
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(addr:Address)
WHERE move.relationship_type = 'LIVES_AT'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     addr, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_addr:LIVES_AT {
  source_system_key: move.source_system_key,
  source_record_pk: move.source_record_pk
}]->(addr)
WHERE survivor_addr.merge_origin_person_id = move.origin_person_id
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  MERGE (absorbed)-[restored_addr:LIVES_AT {
    source_system_key: move.source_system_key,
    source_record_pk: move.source_record_pk
  }]->(addr)
  SET restored_addr = move_props,
      restored_addr.relationship_type = null,
      restored_addr.direction = null,
      restored_addr.origin_person_id = null,
      restored_addr.created_on_survivor = null,
      restored_addr.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_addr IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_addr
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(sr_fact:SourceRecord)
WHERE move.relationship_type = 'HAS_FACT'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     sr_fact, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_fact:HAS_FACT]->(sr_fact)
WHERE survivor_fact.merge_origin_person_id = move.origin_person_id
  AND survivor_fact.attribute_name = move.attribute_name
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  CREATE (absorbed)-[restored_fact:HAS_FACT]->(sr_fact)
  SET restored_fact = move_props,
      restored_fact.relationship_type = null,
      restored_fact.direction = null,
      restored_fact.origin_person_id = null,
      restored_fact.created_on_survivor = null,
      restored_fact.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_fact IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_fact
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(k_other_out:Person)
WHERE move.relationship_type = 'KNOWS_OUT'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     k_other_out, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_k_out:KNOWS]->(k_other_out)
WHERE survivor_k_out.merge_origin_person_id = move.origin_person_id
  AND survivor_k_out.source_record_pk = move.source_record_pk
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND k_other_out.person_id <> absorbed.person_id THEN [1] ELSE [] END |
  CREATE (absorbed)-[restored_k_out:KNOWS]->(k_other_out)
  SET restored_k_out = move_props,
      restored_k_out.relationship_type = null,
      restored_k_out.direction = null,
      restored_k_out.origin_person_id = null,
      restored_k_out.created_on_survivor = null,
      restored_k_out.declared_by_person_id = absorbed.person_id,
      restored_k_out.merge_origin_person_id = move.origin_person_id,
      restored_k_out.updated_at = datetime()
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_k_out IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_k_out
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(k_other_in:Person)
WHERE move.relationship_type = 'KNOWS_IN'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     k_other_in, move, origin, properties(move) AS move_props
OPTIONAL MATCH (k_other_in)-[survivor_k_in:KNOWS]->(current_survivor)
WHERE survivor_k_in.merge_origin_person_id = move.origin_person_id
  AND survivor_k_in.source_record_pk = move.source_record_pk
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND k_other_in.person_id <> absorbed.person_id THEN [1] ELSE [] END |
  CREATE (k_other_in)-[restored_k_in:KNOWS]->(absorbed)
  SET restored_k_in = move_props,
      restored_k_in.relationship_type = null,
      restored_k_in.direction = null,
      restored_k_in.origin_person_id = null,
      restored_k_in.created_on_survivor = null,
      restored_k_in.merge_origin_person_id = move.origin_person_id,
      restored_k_in.updated_at = datetime()
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_k_in IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_k_in
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(order:Order)
WHERE move.relationship_type = 'PURCHASED'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     order, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_context:PURCHASED {
  source_system_key: move.source_system_key,
  source_order_id: move.source_order_id
}]->(order)
WHERE survivor_context.merge_origin_person_id = move.origin_person_id
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  MERGE (absorbed)-[restored_context:PURCHASED {
    source_system_key: move.source_system_key,
    source_order_id: move.source_order_id
  }]->(order)
  SET restored_context = move_props,
      restored_context.relationship_type = null,
      restored_context.direction = null,
      restored_context.origin_person_id = null,
      restored_context.created_on_survivor = null,
      restored_context.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_context.source_record_pk = move.source_record_pk
                         AND survivor_context IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_context
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(vehicle:Vehicle)
WHERE move.relationship_type = 'BOUGHT_VEHICLE'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     vehicle, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_context:BOUGHT_VEHICLE {
  source_system_key: move.source_system_key,
  source_order_id: move.source_order_id
}]->(vehicle)
WHERE survivor_context.merge_origin_person_id = move.origin_person_id
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  MERGE (absorbed)-[restored_context:BOUGHT_VEHICLE {
    source_system_key: move.source_system_key,
    source_order_id: move.source_order_id
  }]->(vehicle)
  SET restored_context = move_props,
      restored_context.relationship_type = null,
      restored_context.direction = null,
      restored_context.origin_person_id = null,
      restored_context.created_on_survivor = null,
      restored_context.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_context.source_record_pk = move.source_record_pk
                         AND survivor_context IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_context
)

WITH DISTINCT absorbed, mi, merge_event, current_survivor, current_survivor_id
OPTIONAL MATCH (merge_event)-[move:MOVED_RELATIONSHIP]->(vehicle:Vehicle)
WHERE move.relationship_type = 'OWNS_VEHICLE'
OPTIONAL MATCH (origin:Person {person_id: move.origin_person_id})
WITH absorbed, mi, merge_event, current_survivor, current_survivor_id,
     vehicle, move, origin, properties(move) AS move_props
OPTIONAL MATCH (current_survivor)-[survivor_context:OWNS_VEHICLE {
  source_system_key: move.source_system_key,
  source_record_pk: move.source_record_pk
}]->(vehicle)
WHERE survivor_context.merge_origin_person_id = move.origin_person_id
FOREACH (_ IN CASE WHEN move IS NOT NULL
                         AND (origin = absorbed OR origin.status = 'merged') THEN [1] ELSE [] END |
  MERGE (absorbed)-[restored_context:OWNS_VEHICLE {
    source_system_key: move.source_system_key,
    source_record_pk: move.source_record_pk
  }]->(vehicle)
  SET restored_context = move_props,
      restored_context.relationship_type = null,
      restored_context.direction = null,
      restored_context.origin_person_id = null,
      restored_context.created_on_survivor = null,
      restored_context.merge_origin_person_id = move.origin_person_id
)
FOREACH (_ IN CASE WHEN move.created_on_survivor = true
                         AND (origin = absorbed OR origin.status = 'merged')
                         AND survivor_context.source_record_pk = move.source_record_pk
                         AND survivor_context IS NOT NULL THEN [1] ELSE [] END |
  DELETE survivor_context
)

WITH DISTINCT absorbed, mi, current_survivor, current_survivor_id
DELETE mi
SET absorbed.status = 'active', absorbed.updated_at = datetime()
WITH absorbed, current_survivor, current_survivor_id, 1 AS removed_count
OPTIONAL MATCH (affected:Person)-[:KNOWS]-(unmerge_neighbor:Person {status: 'active'})
WHERE affected IN [absorbed, current_survivor]
  AND unmerge_neighbor <> absorbed
  AND unmerge_neighbor <> current_survivor
WITH absorbed, current_survivor, current_survivor_id, removed_count,
     collect(DISTINCT unmerge_neighbor) AS unmerge_neighbors
FOREACH (unmerge_neighbor IN unmerge_neighbors |
  SET unmerge_neighbor.analysis_input_revision =
        coalesce(unmerge_neighbor.analysis_input_revision, 0) + 1,
      unmerge_neighbor.analysis_dirty_at = datetime()
)
SET absorbed.analysis_input_revision = coalesce(absorbed.analysis_input_revision, 0) + 1,
    absorbed.analysis_dirty_at = datetime(),
    current_survivor.analysis_input_revision =
      coalesce(current_survivor.analysis_input_revision, 0) + 1,
    current_survivor.analysis_dirty_at = datetime()
RETURN removed_count, current_survivor_id
"""

CREATE_UNMERGE_AUDIT = """
MATCH (absorbed:Person {person_id: $absorbed_id})
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (ume:MergeEvent {
  merge_event_id: randomUUID(),
  event_type: 'unmerge',
  actor_type: 'admin',
  actor_id: $actor_id,
  reason: $reason,
  metadata: $metadata,
  created_at: datetime()
})
CREATE (ume)-[:ABSORBED]->(absorbed)
CREATE (ume)-[:SURVIVOR]->(survivor)
RETURN ume.merge_event_id AS merge_event_id, toString(ume.created_at) AS created_at
"""

FLAG_AFFECTED_RECORDS_FOR_REVIEW = """
MATCH (me:MergeEvent {merge_event_id: $merge_event_id})-[:AFFECTED_RECORD]->(sr:SourceRecord)
SET sr.link_status = 'pending_review'
"""

CHECK_EXISTING_LOCK = """
MATCH (a:Person {person_id: $left})-[lock:NO_MATCH_LOCK]-(b:Person {person_id: $right})
WHERE lock.expires_at IS NULL OR lock.expires_at > datetime()
RETURN lock.lock_id AS lock_id
"""

CREATE_PERSON_PAIR_LOCK = """
MATCH (a:Person {person_id: $left})
MATCH (b:Person {person_id: $right})
CREATE (a)-[lock:NO_MATCH_LOCK {
  lock_id: randomUUID(),
  lock_type: $lock_type,
  reason: $reason,
  actor_type: 'admin',
  actor_id: $actor_id,
  expires_at: CASE WHEN $expires_at IS NOT NULL THEN datetime($expires_at) ELSE null END,
  created_at: datetime()
}]->(b)
RETURN lock.lock_id AS lock_id
"""

DELETE_LOCK = """
MATCH ()-[lock:NO_MATCH_LOCK {lock_id: $lock_id}]->()
DELETE lock
RETURN $lock_id AS deleted_lock_id
"""

CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(a:Person)
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(b:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND (
    (a.person_id = $absorbed_id AND b.person_id = $survivor_id)
    OR (b.person_id = $absorbed_id AND a.person_id = $survivor_id)
  )
SET rc.queue_state = 'cancelled',
    rc.resolution = 'cancelled_superseded',
    rc.resolved_at = datetime(),
    rc.closed_by_merge_event_id = $merge_event_id,
    rc.updated_at = datetime()
"""

REDIRECT_PERSON_PAIR_CASES_ABSORBED_LEFT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[old_left:ABOUT_LEFT {entity_type: 'person'}]->(:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(other:Person)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND other.person_id <> $survivor_id
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(survivor)
DELETE old_left
SET rc.redirected_pair_by_merge_event_id = $merge_event_id,
    rc.redirected_pair_from_person_id = $absorbed_id,
    rc.redirected_pair_side = 'left',
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REDIRECT_PERSON_PAIR_CASES_ABSORBED_RIGHT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
MATCH (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(other:Person)
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person {person_id: $absorbed_id})
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
  AND other.person_id <> $survivor_id
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_pair_by_merge_event_id = $merge_event_id,
    rc.redirected_pair_from_person_id = $absorbed_id,
    rc.redirected_pair_side = 'right',
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REVERT_PERSON_PAIR_REDIRECTS_LEFT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.redirected_pair_by_merge_event_id = $merge_event_id
  AND rc.redirected_pair_side = 'left'
  AND rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[cur_left:ABOUT_LEFT {entity_type: 'person'}]->(:Person)
MATCH (absorbed:Person {person_id: rc.redirected_pair_from_person_id})
CREATE (md)-[:ABOUT_LEFT {entity_type: 'person'}]->(absorbed)
DELETE cur_left
SET rc.redirected_pair_by_merge_event_id = null,
    rc.redirected_pair_from_person_id = null,
    rc.redirected_pair_side = null,
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REVERT_PERSON_PAIR_REDIRECTS_RIGHT = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.redirected_pair_by_merge_event_id = $merge_event_id
  AND rc.redirected_pair_side = 'right'
  AND rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[cur_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person)
MATCH (absorbed:Person {person_id: rc.redirected_pair_from_person_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed)
DELETE cur_right
SET rc.redirected_pair_by_merge_event_id = null,
    rc.redirected_pair_from_person_id = null,
    rc.redirected_pair_side = null,
    rc.updated_at = datetime()
RETURN rc.review_case_id AS review_case_id
"""

REDIRECT_RECORD_PERSON_CASES_FOR_ABSORBED = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[old_right:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed:Person {person_id: $absorbed_id})
MATCH (md)-[:ABOUT_LEFT {entity_type: 'source_record'}]->(:SourceRecord)
MATCH (survivor:Person {person_id: $survivor_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(survivor)
DELETE old_right
SET rc.redirected_by_merge_event_id = $merge_event_id,
    rc.redirected_from_person_id = $absorbed_id,
    rc.updated_at = datetime()
"""

REVERT_RECORD_PERSON_CASE_REDIRECTS = """
MATCH (rc:ReviewCase)-[:FOR_DECISION]->(md:MatchDecision)
WHERE rc.redirected_by_merge_event_id = $merge_event_id
  AND rc.queue_state IN ['open', 'assigned', 'deferred']
MATCH (md)-[cur_right:ABOUT_RIGHT {entity_type: 'person'}]->(:Person)
MATCH (absorbed:Person {person_id: rc.redirected_from_person_id})
CREATE (md)-[:ABOUT_RIGHT {entity_type: 'person'}]->(absorbed)
DELETE cur_right
SET rc.redirected_by_merge_event_id = null,
    rc.redirected_from_person_id = null,
    rc.updated_at = datetime()
"""

REVERT_PERSON_PAIR_CASE_CLOSURES = """
MATCH (rc:ReviewCase)
WHERE rc.closed_by_merge_event_id = $merge_event_id
  AND rc.queue_state = 'cancelled'
  AND rc.resolution = 'cancelled_superseded'
SET rc.queue_state = 'open',
    rc.resolution = null,
    rc.resolved_at = null,
    rc.closed_by_merge_event_id = null,
    rc.updated_at = datetime()
"""
