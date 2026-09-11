"""Cypher constants for the scoped-Identifier schema transition."""

from __future__ import annotations

SHOW_IDENTIFIER_SCOPE_CONSTRAINTS = """
SHOW CONSTRAINTS YIELD name, type, entityType, labelsOrTypes, properties, ownedIndex
RETURN name, type, entityType, labelsOrTypes, properties, ownedIndex
"""

SHOW_IDENTIFIER_SCOPE_INDEXES = """
SHOW INDEXES YIELD name, type, entityType, labelsOrTypes, properties, state, owningConstraint
RETURN name, type, entityType, labelsOrTypes, properties, state, owningConstraint
"""

CREATE_IDENTIFIER_SCOPE_BRIDGE_INDEX = """
CREATE INDEX idx_identifier_scope_norm_type_bridge IF NOT EXISTS
FOR (id:Identifier) ON (id.identifier_scope, id.normalized_value, id.identifier_type)
"""

DROP_IDENTIFIER_SCOPE_BRIDGE_INDEX = "DROP INDEX idx_identifier_scope_norm_type_bridge IF EXISTS"

DROP_LEGACY_IDENTIFIER_SCOPE_INDEX = "DROP INDEX idx_identifier_type_scope_norm IF EXISTS"

CREATE_IDENTIFIER_IDENTITY_SCOPE_UNIQUE = """
CREATE CONSTRAINT identifier_identity_scope_unique IF NOT EXISTS
FOR (id:Identifier)
REQUIRE (id.identifier_type, id.identifier_scope, id.normalized_value) IS UNIQUE
"""
