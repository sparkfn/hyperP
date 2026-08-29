"""Cypher constants for the :User node and auth-related lookups."""

from __future__ import annotations

CREATE_USER_CONSTRAINT = """
CREATE CONSTRAINT user_email_unique IF NOT EXISTS
FOR (u:User) REQUIRE u.email IS UNIQUE
"""

# Upsert a user on Google sign-in. If the node is new, initialise role based
# on whether the email is in the bootstrap-admin list (passed as $bootstrap_admin).
UPSERT_USER_ON_LOGIN = """
MERGE (u:User {email: $email})
ON CREATE SET
  u.google_sub = $google_sub,
  u.display_name = $display_name,
  u.role = CASE WHEN $bootstrap_admin THEN 'admin' ELSE 'first_time' END,
  u.entity_key = null,
  u.created_at = datetime(),
  u.last_login_at = datetime()
ON MATCH SET
  u.google_sub = $google_sub,
  u.display_name = coalesce($display_name, u.display_name),
  u.role = coalesce(u.role, CASE WHEN $bootstrap_admin THEN 'admin' ELSE 'first_time' END),
  u.entity_key = u.entity_key,
  u.last_login_at = datetime()
RETURN u {
  .email, .google_sub, .role, .entity_key, .display_name
} AS user
"""

EXISTING_USER_EMAILS = """
MATCH (u:User)
WHERE u.email IN $emails
RETURN u.email AS email
"""

CREATE_PRE_REGISTERED_USER = """
CREATE (u:User {
  email: $email,
  google_sub: null,
  display_name: null,
  role: $role,
  entity_key: $entity_key,
  created_at: datetime(),
  updated_at: datetime()
})
WITH u, $entity_key AS ek
OPTIONAL MATCH (e:Entity {entity_key: ek})
FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END |
  CREATE (u)-[:EMPLOYEE_OF]->(e)
)
RETURN u {.email, .google_sub, .role, .entity_key, .display_name} AS user
"""

GET_USER_BY_EMAIL = """
MATCH (u:User {email: $email})
RETURN u {.email, .google_sub, .role, .entity_key, .display_name} AS user
"""

LIST_USERS = """
MATCH (u:User)
OPTIONAL MATCH (u)-[:EMPLOYEE_OF]->(e:Entity)
RETURN u {
  .email, .google_sub, .role, .entity_key, .display_name,
  .created_at, .last_login_at
} AS user,
e {.entity_key, .display_name} AS entity
ORDER BY u.email
"""

# Sets role and/or entity_key; also rewires the :EMPLOYEE_OF edge.
# $new_role may be null (keep), $entity_key may be null (clear).
UPDATE_USER = """
MATCH (u:User {email: $email})
OPTIONAL MATCH (u)-[old:EMPLOYEE_OF]->()
DELETE old
SET u.role = coalesce($new_role, u.role),
    u.entity_key = $entity_key,
    u.updated_at = datetime()
WITH u, $entity_key AS ek
OPTIONAL MATCH (e:Entity {entity_key: ek})
FOREACH (_ IN CASE WHEN e IS NOT NULL THEN [1] ELSE [] END |
  MERGE (u)-[:EMPLOYEE_OF]->(e)
)
RETURN u {.email, .google_sub, .role, .entity_key, .display_name} AS user
"""

# Resolve a source_key to all source- and record-scoped entities for authorization.
GET_ENTITY_FOR_SOURCE = """
MATCH (ss:SourceSystem {source_key: $source_key})
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
WITH ss, collect(DISTINCT source_entity.entity_key) AS source_entity_keys
CALL (ss, source_entity_keys) {
  WITH ss, source_entity_keys
  WHERE size(source_entity_keys) = 0
  OPTIONAL MATCH (record:SourceRecord)-[:FROM_SOURCE]->(ss)
  OPTIONAL MATCH (record)-[:OWNED_BY]->(record_entity:Entity)
  RETURN collect(DISTINCT record_entity.entity_key) AS record_entity_keys
  UNION
  WITH source_entity_keys
  WHERE size(source_entity_keys) > 0
  RETURN [] AS record_entity_keys
}
RETURN source_entity_keys + record_entity_keys AS entity_keys
"""

# Resolve a review case to the set of entities its comparison persons touch.
GET_ENTITIES_FOR_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(l)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(r)
WITH collect(DISTINCT l) + collect(DISTINCT r) AS sides
UNWIND sides AS node
OPTIONAL MATCH (node)<-[source_link:LINKED_TO]-(sr:SourceRecord)
WHERE coalesce(source_link.is_active, true) = true
OPTIONAL MATCH (node)-[person_link:LINKED_TO]->(p:Person)
               <-[person_source_link:LINKED_TO]-(sr2:SourceRecord)
WHERE coalesce(person_link.is_active, true) = true
  AND coalesce(person_source_link.is_active, true) = true
WITH collect(DISTINCT sr) + collect(DISTINCT sr2) AS srs
UNWIND srs AS sr
OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)
OPTIONAL MATCH (sr)-[:OWNED_BY]->(record_entity:Entity)
OPTIONAL MATCH (ss)-[:OPERATED_BY]->(source_entity:Entity)
WITH coalesce(record_entity, source_entity) AS e
RETURN collect(DISTINCT e.entity_key) AS entity_keys
"""

# Resolve a report's owning entity if the report was scoped to one.
GET_ENTITY_FOR_ENTITY_KEY = """
MATCH (e:Entity {entity_key: $entity_key})
RETURN e.entity_key AS entity_key
"""
