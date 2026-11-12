"""Cypher for the Neo4j -> Postgres read projection (#491).

Every statement here is read-only: the projection never writes to Neo4j, it
only mirrors graph state into the disposable Postgres projection owned by the
Rust read spike. All Cypher for the projection lives in this module so the
orchestration modules stay free of query text.

The list and detail projections deliberately return the same record shape the
API returns, so the projected payloads match the API documents field for
field.

All timestamp comparisons go through ``datetime($mark)`` with an ISO-8601 UTC
mark, because the graph stores zoned ``datetime()`` values and Neo4j refuses
to compare a zoned value with an offset value.
"""

from __future__ import annotations

# --- Change detection ------------------------------------------------------

# Change signals the ingestion and API write paths actually stamp:
#   * Person.updated_at (golden profile recompute, merge, retirement)
#   * IDENTIFIED_BY / LIVES_AT rels (last_seen_at, last_confirmed_at,
#     updated_at)
#   * LINKED_TO rels (linked_at) and their SourceRecord nodes (ingested_at,
#     updated_at)
#   * KNOWS rels (last_seen_at, updated_at)
#   * PURCHASED rels (last_seen_at, created_at)
#   * HAS_BANKRUPTCY_CASE rels (last_seen_at, updated_at)
#   * MatchDecision.created_at and ReviewCase.updated_at
# Entity display names carry no timestamp, so an entity rename is a known gap.
PROJECTION_CHANGED_PERSON_IDS = """
MATCH (p:Person)
WHERE p.person_id > $after_person_id
  AND (
    p.updated_at >= datetime($mark)
    OR EXISTS {
      MATCH (p)-[r:IDENTIFIED_BY]->(:Identifier)
      WHERE r.last_seen_at >= datetime($mark)
        OR r.last_confirmed_at >= datetime($mark)
        OR r.updated_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (p)-[r:LIVES_AT]->(:Address)
      WHERE r.last_seen_at >= datetime($mark)
        OR r.last_confirmed_at >= datetime($mark)
        OR r.updated_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (p)<-[r:LINKED_TO]-(:SourceRecord)
      WHERE r.linked_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (sr:SourceRecord)-[r:LINKED_TO]->(p)
      WHERE sr.ingested_at >= datetime($mark)
        OR sr.updated_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (p)-[r:KNOWS]-(:Person)
      WHERE r.last_seen_at >= datetime($mark)
        OR r.updated_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (p)-[r:PURCHASED]->(:Order)
      WHERE r.last_seen_at >= datetime($mark)
        OR r.created_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (p)-[r:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)
      WHERE r.last_seen_at >= datetime($mark)
        OR r.updated_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
      WHERE md.created_at >= datetime($mark)
    }
    OR EXISTS {
      MATCH (rc:ReviewCase)-[:FOR_DECISION]->(:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
      WHERE rc.updated_at >= datetime($mark)
    }
  )
RETURN p.person_id AS person_id
ORDER BY p.person_id
LIMIT $page_size
"""

# One-hop neighbours of changed persons through shared Identifier/Address
# nodes and KNOWS edges. possible_match_count and connection_count depend on
# these neighbours, so they must be rebuilt as well.
PROJECTION_NEIGHBOUR_PERSON_IDS = """
UNWIND $person_ids AS seed_id
MATCH (p:Person {person_id: seed_id})
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ident_neighbour:Person)
  WHERE ident_neighbour.person_id <> p.person_id
    AND ident_neighbour.status <> 'merged'
WITH p, collect(DISTINCT ident_neighbour.person_id) AS ident_ids
OPTIONAL MATCH (p)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(addr_neighbour:Person)
  WHERE addr_neighbour.person_id <> p.person_id
    AND addr_neighbour.status <> 'merged'
WITH p, ident_ids, collect(DISTINCT addr_neighbour.person_id) AS addr_ids
OPTIONAL MATCH (p)-[knows_rel:KNOWS]-(knows_neighbour:Person)
  WHERE knows_neighbour.person_id <> p.person_id
    AND knows_neighbour.status <> 'merged'
    AND coalesce(knows_rel.is_active, true) = true
WITH p, ident_ids, addr_ids, collect(DISTINCT knows_neighbour.person_id) AS knows_ids
UNWIND ident_ids + addr_ids + knows_ids AS neighbour_id
RETURN DISTINCT neighbour_id AS person_id
ORDER BY person_id
"""

# --- Keyset paging over the whole Person id space --------------------------

PROJECTION_ALL_PERSON_IDS = """
MATCH (p:Person)
WHERE p.person_id > $after_person_id
RETURN p.person_id AS person_id
ORDER BY p.person_id
LIMIT $page_size
"""

PROJECTION_PERSON_COUNT = """
MATCH (p:Person)
RETURN count(p) AS person_count
"""

PROJECTION_EXISTING_PERSON_IDS = """
MATCH (p:Person)
WHERE p.person_id IN $person_ids
RETURN p.person_id AS person_id
ORDER BY p.person_id
"""

# --- Person projection rows -------------------------------------------------

# LIST view, mirroring the API person listing row:
#   * source_record_count uses the history_family (NULL or 'activity') and
#     effective-active predicate.
#   * connection_count uses shared-Address persons plus KNOWS neighbours.
#   * entities carry the per-entity source-record count of the same predicate.
PROJECTION_PERSON_LIST_ROWS = """
UNWIND $person_ids AS person_id
MATCH (p:Person {person_id: person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
OPTIONAL MATCH (addr:Address {address_id: p.preferred_address_id})
CALL {
  WITH p
  RETURN count {
    (sr:SourceRecord)-[link:LINKED_TO]->(p)
    WHERE coalesce(link.is_active, true) = true
      AND (sr.history_family IS NULL OR sr.history_family = 'activity')
      AND (sr.lifecycle_status = 'active'
        OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
  } AS source_record_count
}
CALL {
  WITH p
  OPTIONAL MATCH (p)-[p_addr:LIVES_AT]->(:Address)<-[c_addr:LIVES_AT]-(ca:Person)
    WHERE coalesce(p_addr.is_active, true) = true
      AND coalesce(c_addr.is_active, true) = true
      AND ca.person_id <> p.person_id
      AND ca.status <> 'merged'
  WITH p, collect(DISTINCT ca) AS address_conn
  OPTIONAL MATCH (p)-[p_knows:KNOWS]-(ck:Person)
    WHERE coalesce(p_knows.is_active, true) = true
      AND ck.person_id <> p.person_id
      AND ck.status <> 'merged'
  WITH address_conn, collect(DISTINCT ck) AS knows_conn
  WITH address_conn + knows_conn AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
CALL {
  WITH p
  OPTIONAL MATCH (p)-[pi:IDENTIFIED_BY]->(phone_id:Identifier)
    WHERE coalesce(pi.is_active, true) = true
      AND phone_id.identifier_type = 'phone'
      AND phone_id.normalized_value = p.preferred_phone
  WITH pi.quality_flag AS qf
  ORDER BY CASE qf WHEN 'valid' THEN 0 ELSE 1 END
  LIMIT 1
  RETURN CASE qf
    WHEN 'valid' THEN 1.0
    WHEN 'partial_parse' THEN 0.8
    WHEN 'stale' THEN 0.6
    WHEN 'source_untrusted' THEN 0.4
    WHEN 'shared_suspected' THEN 0.3
    WHEN 'placeholder_value' THEN 0.1
    WHEN 'invalid_format' THEN 0.0
    ELSE null
  END AS phone_confidence
}
CALL {
  WITH p
  OPTIONAL MATCH (sr_ent:SourceRecord)-[link:LINKED_TO]->(p)
    WHERE coalesce(link.is_active, true) = true
      AND (sr_ent.history_family IS NULL OR sr_ent.history_family = 'activity')
      AND (sr_ent.lifecycle_status = 'active'
        OR (sr_ent.lifecycle_status IS NULL AND sr_ent.is_latest = true))
  OPTIONAL MATCH (sr_ent)-[:FROM_SOURCE]->(ss_ent:SourceSystem)
  OPTIONAL MATCH (sr_ent)-[:OWNED_BY]->(record_entity:Entity)
  OPTIONAL MATCH (ss_ent)-[:OPERATED_BY]->(source_entity:Entity)
  WITH coalesce(record_entity, source_entity) AS e, sr_ent
  WITH e, count(DISTINCT sr_ent) AS e_sr_count
  WHERE e IS NOT NULL
  WITH collect({
    entity_key: e.entity_key,
    display_name: e.display_name,
    entity_type: e.entity_type,
    country_code: e.country_code,
    is_active: e.is_active,
    source_record_count: e_sr_count
  }) AS entities
  RETURN entities, size(entities) AS entity_count
}
CALL {
  WITH p
  RETURN count {
    (p)-[id_count:IDENTIFIED_BY]->(:Identifier)
    WHERE coalesce(id_count.is_active, true) = true
  } AS identifier_count
}
CALL {
  WITH p
  OPTIONAL MATCH (p)-[p_shared_id:IDENTIFIED_BY]->(shared_id:Identifier)
    WHERE coalesce(p_shared_id.is_active, true) = true
  WITH DISTINCT p, shared_id
  OPTIONAL MATCH (shared_id)<-[other_shared_id:IDENTIFIED_BY]-(other:Person)
    WHERE shared_id IS NOT NULL
      AND coalesce(other_shared_id.is_active, true) = true
      AND other.person_id <> p.person_id
      AND other.status <> 'merged'
  WITH DISTINCT other
  RETURN count(other) AS possible_match_count
}
CALL {
  WITH p
  OPTIONAL MATCH (md:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(p)
    WHERE EXISTS { (md)-[:ABOUT_LEFT]->(:Person) }
      AND EXISTS { (md)-[:ABOUT_RIGHT]->(:Person) }
      AND EXISTS {
        (rc:ReviewCase)-[:FOR_DECISION]->(md)
        WHERE NOT rc.queue_state IN ['resolved', 'cancelled']
      }
  RETURN count(DISTINCT md) AS system_match_count
}
CALL {
  WITH p
  RETURN count {
    (p)-[purchase:PURCHASED]->(:Order)
    WHERE coalesce(purchase.is_active, true) = true
  } AS order_count
}
CALL {
  WITH p
  RETURN count {
    (p)-[bankruptcy_rel:HAS_BANKRUPTCY_CASE]->(:BankruptcyCase)
    WHERE coalesce(bankruptcy_rel.is_active, true) = true
  } AS bankruptcy_case_count
}
RETURN person_id,
       canonical.person_id AS canonical_id,
       p {
         .person_id, .status, .is_high_value, .is_high_risk,
         .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
         .preferred_nric, .profile_completeness_score, .golden_profile_computed_at,
         .golden_profile_version, .created_at, .updated_at
       } AS person,
       addr {
         .address_id, .unit_number, .street_number, .street_name,
         .city, .postal_code, .country_code, .normalized_full
       } AS preferred_address,
       source_record_count,
       connection_count,
       phone_confidence,
       entities,
       entity_count,
       identifier_count,
       possible_match_count,
       system_match_count,
       order_count,
       bankruptcy_case_count,
       p.crm_deal_count AS crm_deal_count,
       p.preferred_address_id AS preferred_address_id
"""

# DETAIL view, mirroring GET /v1/persons/{id}: the canonical person wins (one
# MERGED_INTO hop) and connection_count also counts persons sharing an
# Identifier.
PROJECTION_PERSON_DETAIL_ROWS = """
UNWIND $person_ids AS person_id
MATCH (p:Person {person_id: person_id})
OPTIONAL MATCH (p)-[:MERGED_INTO]->(canonical:Person)
WITH person_id, canonical.person_id AS canonical_id, coalesce(canonical, p) AS person
OPTIONAL MATCH (addr:Address {address_id: person.preferred_address_id})
WITH person_id, canonical_id, person, addr, count {
  (sr:SourceRecord)-[link:LINKED_TO]->(person)
  WHERE coalesce(link.is_active, true) = true
    AND (sr.history_family IS NULL OR sr.history_family = 'activity')
    AND (sr.lifecycle_status = 'active'
      OR (sr.lifecycle_status IS NULL AND sr.is_latest = true))
} AS source_record_count
CALL {
  WITH person
  OPTIONAL MATCH (person)-[person_identifier:IDENTIFIED_BY]->(:Identifier)
    <-[ci_identifier:IDENTIFIED_BY]-(ci:Person)
    WHERE coalesce(person_identifier.is_active, true) = true
      AND coalesce(ci_identifier.is_active, true) = true
      AND ci.person_id <> person.person_id
      AND ci.status <> 'merged'
  WITH person, collect(DISTINCT ci) AS identifier_conn
  OPTIONAL MATCH (person)-[person_address:LIVES_AT]->(:Address)
    <-[ca_address:LIVES_AT]-(ca:Person)
    WHERE coalesce(person_address.is_active, true) = true
      AND coalesce(ca_address.is_active, true) = true
      AND ca.person_id <> person.person_id
      AND ca.status <> 'merged'
  WITH person, identifier_conn, collect(DISTINCT ca) AS address_conn
  OPTIONAL MATCH (person)-[person_knows:KNOWS]-(ck:Person)
    WHERE coalesce(person_knows.is_active, true) = true
      AND ck.person_id <> person.person_id
      AND ck.status <> 'merged'
  WITH identifier_conn, address_conn, collect(DISTINCT ck) AS knows_conn
  WITH identifier_conn + address_conn + knows_conn AS all_conn
  UNWIND all_conn AS c
  RETURN count(DISTINCT c) AS connection_count
}
CALL {
  WITH person
  OPTIONAL MATCH (person)-[purchase:PURCHASED]->(o:Order)
    WHERE coalesce(purchase.is_active, true) = true
  RETURN sum(o.total_amount) AS lifetime_value
}
RETURN person_id AS requested_person_id,
       canonical_id,
       person {
         .person_id, .status, .is_high_value, .is_high_risk,
         .preferred_full_name, .preferred_phone, .preferred_email, .preferred_dob,
         .preferred_nric, .preferred_race_ethnicity, .profile_completeness_score,
         .golden_profile_computed_at, .golden_profile_version, .created_at, .updated_at
       } AS person,
       addr {
         .address_id, .unit_number, .street_number, .street_name,
         .city, .postal_code, .country_code, .normalized_full
       } AS preferred_address,
       source_record_count,
       connection_count,
       lifetime_value
"""

# --- Child collections ------------------------------------------------------

PROJECTION_PERSON_IDENTIFIERS = """
UNWIND $person_ids AS person_id
MATCH (p:Person {person_id: person_id})-[rel:IDENTIFIED_BY]->(id:Identifier)
WITH person_id, id, rel
ORDER BY coalesce(rel.source_record_pk, ''), coalesce(rel.source_system_key, '')
WITH person_id, id,
     any(r IN collect(rel) WHERE r.is_active = true) AS is_active,
     any(r IN collect(rel) WHERE r.is_verified = true) AS is_verified,
     max(rel.last_confirmed_at) AS last_confirmed_at,
     head([r IN collect(rel) WHERE r.source_system_key IS NOT NULL | r.source_system_key])
       AS source_system_key
RETURN person_id AS person_id,
       id.identifier_type AS identifier_type,
       id.normalized_value AS normalized_value,
       is_active,
       is_verified,
       last_confirmed_at,
       source_system_key
ORDER BY identifier_type, normalized_value
"""

PROJECTION_PERSON_ADDRESSES = """
UNWIND $person_ids AS person_id
MATCH (p:Person {person_id: person_id})-[rel:LIVES_AT]->(addr:Address)
WITH person_id, addr, collect(coalesce(rel.is_active, true)) AS active_flags
RETURN person_id AS person_id,
       addr.address_id AS address_id,
       any(flag IN active_flags WHERE flag = true) AS is_active,
       addr.unit_number AS unit_number,
       addr.street_number AS street_number,
       addr.street_name AS street_name,
       addr.city AS city,
       addr.postal_code AS postal_code,
       addr.country_code AS country_code,
       addr.normalized_full AS normalized_full
ORDER BY address_id
"""

PROJECTION_PERSON_CONNECTIONS = """
UNWIND $person_ids AS person_id
MATCH (p:Person {person_id: person_id})
OPTIONAL MATCH (p)-[:IDENTIFIED_BY]->(:Identifier)<-[:IDENTIFIED_BY]-(ident_neighbour:Person)
  WHERE ident_neighbour.person_id <> p.person_id
    AND ident_neighbour.status <> 'merged'
WITH person_id, p, collect(DISTINCT ident_neighbour) AS ident_conn
OPTIONAL MATCH (p)-[:LIVES_AT]->(:Address)<-[:LIVES_AT]-(addr_neighbour:Person)
  WHERE addr_neighbour.person_id <> p.person_id
    AND addr_neighbour.status <> 'merged'
WITH person_id, p, ident_conn, collect(DISTINCT addr_neighbour) AS addr_conn
OPTIONAL MATCH (p)-[knows_rel:KNOWS]-(knows_neighbour:Person)
  WHERE knows_neighbour.person_id <> p.person_id
    AND knows_neighbour.status <> 'merged'
    AND coalesce(knows_rel.is_active, true) = true
WITH person_id, p, ident_conn, addr_conn, collect(DISTINCT knows_neighbour) AS knows_conn
UNWIND ident_conn + addr_conn + knows_conn AS neighbour
WITH person_id, neighbour,
     CASE WHEN neighbour IN knows_conn THEN 'knows'
          WHEN neighbour IN addr_conn THEN 'address'
          ELSE 'identifier' END AS connection_type
RETURN person_id AS person_id,
       neighbour.person_id AS connected_person_id,
       1 AS hops,
       connection_type
ORDER BY connected_person_id
"""

# Every LINKED_TO record is projected (active or not) so the Rust reader can
# filter on the flags. The history_family exclusion applies only to the API
# counters, not to the filter rows.
PROJECTION_PERSON_SOURCE_RECORDS = """
UNWIND $person_ids AS person_id
MATCH (sr:SourceRecord)-[rel:LINKED_TO]->(p:Person {person_id: person_id})
MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
RETURN sr.source_record_pk AS source_record_pk,
       person_id AS person_id,
       coalesce(record_entity.entity_key, source_entity.entity_key) AS entity_key,
       ss.source_key AS source_key,
       sr.record_type AS record_type,
       CASE
         WHEN sr.lifecycle_status = 'active'
           OR (sr.lifecycle_status IS NULL AND sr.is_latest = true) THEN 'active'
         ELSE coalesce(sr.lifecycle_status, 'inactive')
       END AS lifecycle_status,
       coalesce(rel.is_active, true) AS is_active,
       sr.observed_at AS observed_at,
       sr.source_record_id AS source_record_id,
       sr.source_record_version AS source_record_version,
       sr.link_status AS link_status,
       sr.ingested_at AS ingested_at
ORDER BY source_record_pk, person_id
"""

# --- Summary counters -------------------------------------------------------

PROJECTION_SUMMARY_COUNTERS = """
CALL {
  MATCH (p:Person)
  WHERE p.status <> 'merged'
  RETURN count(p) AS all_profiles_count,
         sum(CASE WHEN p.is_high_risk = true THEN 1 ELSE 0 END) AS high_risk_count,
         sum(CASE WHEN p.is_high_value = true THEN 1 ELSE 0 END) AS high_value_count,
         sum(CASE WHEN p.preferred_phone IS NULL AND p.preferred_email IS NULL THEN 1 ELSE 0 END)
           AS no_contact_count
}
CALL {
  MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[deal_link:LINKED_TO]->(p:Person)
  WHERE p.status <> 'merged'
    AND coalesce(deal_link.is_active, true) = true
    AND (deal.lifecycle_status = 'active'
      OR (deal.lifecycle_status IS NULL AND deal.is_latest = true))
  RETURN count(DISTINCT CASE
           WHEN deal.observed_at >= datetime.truncate('month', datetime($bound)) THEN deal
         END) AS deals_this_month_count,
         count(DISTINCT deal) AS all_deals_count
}
RETURN all_profiles_count,
       coalesce(high_risk_count, 0) AS high_risk_count,
       coalesce(high_value_count, 0) AS high_value_count,
       coalesce(no_contact_count, 0) AS no_contact_count,
       coalesce(deals_this_month_count, 0) AS deals_this_month_count,
       coalesce(all_deals_count, 0) AS all_deals_count
"""

# --- Identity-link revisions ------------------------------------------------

# Only the 15 public allow-listed fields are projected; raw and normalized
# evidence never leaves the graph.
PROJECTION_IDENTITY_LINK_REVISIONS = """
MATCH (rev:IdentityLinkRevision)
WHERE rev.global_revision > $after_revision
RETURN rev.event_id AS event_id,
       rev.global_revision AS global_revision,
       rev.link_key AS link_key,
       rev.source_system AS source_system,
       rev.source_instance_id AS source_instance_id,
       rev.source_entity_type AS source_entity_type,
       rev.source_entity_id AS source_entity_id,
       rev.identity_policy_version AS identity_policy_version,
       rev.link_status AS link_status,
       rev.hyperp_person_id AS hyperp_person_id,
       rev.resolution_kind AS resolution_kind,
       rev.resolution_revision AS resolution_revision,
       rev.effective_at AS effective_at,
       rev.match_decision_id AS match_decision_id,
       rev.review_case_id AS review_case_id,
       rev.supersedes_event_id AS supersedes_event_id
ORDER BY rev.global_revision ASC
LIMIT $page_size
"""

# Read-only readiness probe (the API variant MERGEs a counter, which the
# projection must not do). The stream is ready only when the counter baseline
# and the baseline migration are both completed.
PROJECTION_IDENTITY_LINK_STATE = """
OPTIONAL MATCH (counter:IdentityLinkRevisionCounter {stream_key: 'identity_link_revision_stream_v1'})
OPTIONAL MATCH (migration:DataMigration {migration_key: 'identity_link_revision_baseline_v2'})
RETURN coalesce(counter.current_revision, 0) AS current_revision,
       counter.baseline_completed_at AS baseline_completed_at,
       migration.completed_at AS migration_completed_at
"""
