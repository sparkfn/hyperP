"""Cypher query for staging graph reset."""

from __future__ import annotations

STAGING_RESET_CLEAR_GRAPH = (
    "MATCH (n) "
    "WHERE NOT n:Entity AND NOT n:SourceSystem "
    "AND NOT n:OAuthClient AND NOT n:OAuthClientSecret "
    "WITH n LIMIT $batch_size "
    "DETACH DELETE n "
    "RETURN count(*) AS deleted"
)
