"""Cypher constants for admin endpoints (source-system listing, field-trust config)."""

from __future__ import annotations

LIST_SOURCE_SYSTEMS = """
MATCH (ss:SourceSystem)
RETURN ss {
  .source_system_id, .source_key, .display_name,
  .system_type, .is_active, .field_trust,
  .created_at, .updated_at
} AS source_system
ORDER BY ss.source_key
"""

GET_FIELD_TRUST = """
MATCH (ss:SourceSystem {source_key: $source_key})
RETURN ss.field_trust AS field_trust,
       ss.source_key AS source_key,
       ss.display_name AS display_name
"""

UPDATE_FIELD_TRUST = """
MATCH (ss:SourceSystem {source_key: $source_key})
SET ss.field_trust = $field_trust,
    ss.updated_at = datetime()
"""
