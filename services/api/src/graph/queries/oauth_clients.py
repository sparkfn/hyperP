"""Cypher constants for OAuth client management."""

from __future__ import annotations

CREATE_OAUTH_CLIENT_ID_CONSTRAINT = """
CREATE CONSTRAINT oauth_client_id_unique IF NOT EXISTS
FOR (c:OAuthClient) REQUIRE c.client_id IS UNIQUE
"""

CREATE_OAUTH_SECRET_ID_CONSTRAINT = """
CREATE CONSTRAINT oauth_secret_id_unique IF NOT EXISTS
FOR (s:OAuthClientSecret) REQUIRE s.secret_id IS UNIQUE
"""

CREATE_OAUTH_CLIENT_WITH_SECRET = """
MERGE (c:OAuthClient {client_id: $client_id})
ON CREATE SET
  c.name         = $name,
  c.entity_key   = $entity_key,
  c.scopes       = $scopes,
  c.created_by   = $created_by,
  c.created_at   = datetime($created_at),
  c.disabled_at  = NULL,
  c.last_used_at = NULL
CREATE (s:OAuthClientSecret {
  secret_id: $secret_id,
  secret_hash: $secret_hash,
  secret_prefix: $secret_prefix,
  created_at: datetime($secret_created_at),
  expires_at: CASE WHEN $secret_expires_at IS NOT NULL THEN datetime($secret_expires_at) ELSE NULL END,
  revoked_at: NULL,
  last_used_at: NULL
})
CREATE (c)-[:HAS_SECRET]->(s)
"""

GET_OAUTH_CLIENTS_FOR_ADMIN = """
MATCH (c:OAuthClient)
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
WITH c, s ORDER BY s.created_at DESC
WITH c, collect(s {
  .secret_id, .secret_prefix, .created_at, .expires_at,
  .revoked_at, .last_used_at
}) AS secrets
WITH c, secrets ORDER BY c.created_at DESC
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at,
  secrets: secrets
} AS client
"""

GET_OAUTH_CLIENT_FOR_VALIDATION = """
MATCH (c:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret)
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at,
  secret: s {
    .secret_id, .secret_hash, .secret_prefix, .created_at,
    .expires_at, .revoked_at, .last_used_at
  }
} AS client
"""

GET_OAUTH_CLIENT_BY_ID = """
MATCH (c:OAuthClient {client_id: $client_id})
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
WITH c, s ORDER BY s.created_at DESC
WITH c, collect(s {
  .secret_id, .secret_prefix, .created_at, .expires_at,
  .revoked_at, .last_used_at
}) AS secrets
RETURN c {
  .client_id, .name, .entity_key, .scopes, .created_by,
  .created_at, .disabled_at, .last_used_at,
  secrets: secrets
} AS client
"""

CREATE_OAUTH_CLIENT_SECRET = """
MATCH (c:OAuthClient {client_id: $client_id})
WHERE c.disabled_at IS NULL
CREATE (s:OAuthClientSecret {
  secret_id: $secret_id,
  secret_hash: $secret_hash,
  secret_prefix: $secret_prefix,
  created_at: datetime($created_at),
  expires_at: CASE WHEN $expires_at IS NOT NULL THEN datetime($expires_at) ELSE NULL END,
  revoked_at: NULL,
  last_used_at: NULL
})
CREATE (c)-[:HAS_SECRET]->(s)
RETURN s.secret_id AS secret_id
"""

REVOKE_OAUTH_CLIENT_SECRET = """
MATCH (:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret {secret_id: $secret_id})
WHERE s.revoked_at IS NULL
SET s.revoked_at = datetime()
RETURN s.secret_id AS secret_id
"""

DISABLE_OAUTH_CLIENT = """
MATCH (c:OAuthClient {client_id: $client_id})
WHERE c.disabled_at IS NULL
SET c.disabled_at = datetime()
RETURN c.client_id AS client_id
"""

DELETE_OAUTH_CLIENT = """
MATCH (c:OAuthClient {client_id: $client_id})
OPTIONAL MATCH (c)-[:HAS_SECRET]->(s:OAuthClientSecret)
DETACH DELETE s, c
"""

UPDATE_OAUTH_CLIENT_LAST_USED = """
MATCH (c:OAuthClient {client_id: $client_id})
SET c.last_used_at = datetime()
"""

UPDATE_OAUTH_SECRET_LAST_USED = """
MATCH (:OAuthClient {client_id: $client_id})-[:HAS_SECRET]->(s:OAuthClientSecret {secret_id: $secret_id})
SET s.last_used_at = datetime()
"""
