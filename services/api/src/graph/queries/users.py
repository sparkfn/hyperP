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
  u.last_login_at = datetime()
RETURN u {
  .email, .google_sub, .role, .entity_key, .display_name
} AS user
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

# Resolve a source_key to its operating entity for authorization.
GET_ENTITY_FOR_SOURCE = """
MATCH (ss:SourceSystem {source_key: $source_key})-[:OPERATED_BY]->(e:Entity)
RETURN e.entity_key AS entity_key
"""

# Resolve a review case to the set of entities its comparison persons touch.
GET_ENTITIES_FOR_REVIEW_CASE = """
MATCH (rc:ReviewCase {review_case_id: $review_case_id})-[:FOR_DECISION]->(md:MatchDecision)
OPTIONAL MATCH (md)-[:ABOUT_LEFT]->(l)
OPTIONAL MATCH (md)-[:ABOUT_RIGHT]->(r)
WITH collect(DISTINCT l) + collect(DISTINCT r) AS sides
UNWIND sides AS node
OPTIONAL MATCH (node)<-[:LINKED_TO]-(sr:SourceRecord)
OPTIONAL MATCH (node)-[:LINKED_TO]->(p:Person)<-[:LINKED_TO]-(sr2:SourceRecord)
WITH collect(DISTINCT sr) + collect(DISTINCT sr2) AS srs
UNWIND srs AS sr
OPTIONAL MATCH (sr)-[:FROM_SOURCE]->(ss:SourceSystem)-[:OPERATED_BY]->(e:Entity)
RETURN collect(DISTINCT e.entity_key) AS entity_keys
"""

# Resolve a report's owning entity if the report was scoped to one.
GET_ENTITY_FOR_ENTITY_KEY = """
MATCH (e:Entity {entity_key: $entity_key})
RETURN e.entity_key AS entity_key
"""
