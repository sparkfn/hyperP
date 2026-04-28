"""Cypher constants for API key management."""

from __future__ import annotations

CREATE_API_KEY_CONSTRAINT = """
CREATE CONSTRAINT api_key_id_unique IF NOT EXISTS
FOR (k:ApiKey) REQUIRE k.id IS UNIQUE
"""

# Create a new ApiKey node. Hash is stored instead of the plain secret.
CREATE_API_KEY_NODE = """
MERGE (k:ApiKey {id: $id})
ON CREATE SET
  k.prefix        = $prefix,
  k.key_hash      = $key_hash,
  k.name          = $name,
  k.entity_key    = $entity_key,
  k.scopes        = $scopes,
  k.created_by    = $created_by,
  k.created_at    = datetime($created_at),
  k.expires_at    = CASE WHEN $expires_at IS NOT NULL THEN datetime($expires_at) ELSE NULL END,
  k.last_used_at  = NULL,
  k.is_revoked    = false
RETURN k {
    .id, .prefix, .name, .entity_key, .scopes,
    .created_by, .created_at, .expires_at, .last_used_at, .is_revoked
} AS key
"""

# Find by stored hash. Used for validation.
GET_API_KEY_BY_PREFIX_HASH = """
MATCH (k:ApiKey {key_hash: $key_hash})
RETURN k {
    .id, .prefix, .name, .entity_key, .scopes,
    .created_by, .created_at, .expires_at, .last_used_at, .is_revoked
} AS key
"""

# Find by id for revocation / deletion.
GET_API_KEY_BY_ID = """
MATCH (k:ApiKey {id: $id})
RETURN k {
    .id, .prefix, .name, .entity_key, .scopes,
    .created_by, .created_at, .expires_at, .last_used_at, .is_revoked
} AS key
"""

# List all keys for admin UI.
GET_API_KEYS_FOR_ADMIN = """
MATCH (k:ApiKey)
WHERE k.is_revoked = false
RETURN k {
    .id, .prefix, .name, .entity_key, .scopes,
    .created_by, .created_at, .expires_at, .last_used_at, .is_revoked
} AS key
ORDER BY k.created_at DESC
"""

# Soft-revoke: mark as revoked. The hash remains but validation will reject it.
REVOKE_API_KEY = """
MATCH (k:ApiKey {id: $id})
SET k.is_revoked = true, k.revoked_at = datetime()
"""

# Hard-delete.
DELETE_API_KEY = """
MATCH (k:ApiKey {id: $id})
DELETE k
"""

# Touch last_used_at after successful validation.
UPDATE_API_KEY_LAST_USED = """
MATCH (k:ApiKey {id: $id})
SET k.last_used_at = datetime()
"""
