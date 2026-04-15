"""Cypher for Entity nodes and Entity-scoped SourceSystem seeding.

An ``Entity`` groups one or more ``SourceSystem`` nodes that belong to the
same real-world organisation (Fundbox, SpeedZone, Eko, …).  Query constants
are idempotent: re-running them on an already-seeded graph is a no-op.
"""

from __future__ import annotations

#: Idempotent create of an Entity node keyed by ``entity_key``.  Display
#: metadata is updated on every run so renames in code propagate forward.
UPSERT_ENTITY = """
MERGE (e:Entity {entity_key: $entity_key})
ON CREATE SET
    e.entity_id     = randomUUID(),
    e.created_at    = datetime()
SET
    e.display_name  = $display_name,
    e.entity_type   = $entity_type,
    e.country_code  = $country_code,
    e.is_active     = true,
    e.updated_at    = datetime()
RETURN e.entity_id AS entity_id
"""

#: Idempotent create of a SourceSystem node attached to its owning Entity.
#:
#: Field trust is passed as a native map so per-source overrides stay
#: data-driven instead of being hard-coded in main.py's seeder.
UPSERT_SOURCE_SYSTEM_WITH_ENTITY = """
MATCH (e:Entity {entity_key: $entity_key})
MERGE (ss:SourceSystem {source_key: $source_key})
ON CREATE SET
    ss.source_system_id = randomUUID(),
    ss.created_at       = datetime()
SET
    ss.display_name = $display_name,
    ss.system_type  = $system_type,
    ss.is_active    = true,
    ss.field_trust  = $field_trust,
    ss.updated_at   = datetime()
MERGE (ss)-[:OPERATED_BY]->(e)
RETURN ss.source_system_id AS source_system_id
"""
