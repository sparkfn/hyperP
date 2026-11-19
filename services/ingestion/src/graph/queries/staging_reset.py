"""Cypher query for staging graph reset.

The reset clears every HyperP data node. The system configuration nodes and
the person-list summary counter are preserved: the counter survives the reset
so its identity (and its generation) stays stable, and it is invalidated by
the reset tool before the first deletion batch.
"""

from __future__ import annotations

STAGING_RESET_CLEAR_GRAPH = (
    "MATCH (n) "
    "WHERE NOT n:Entity AND NOT n:SourceSystem "
    "AND NOT n:User "
    "AND NOT n:OAuthClient AND NOT n:OAuthClientSecret "
    "AND NOT n:PersonListSummaryCounter "
    "WITH n LIMIT $batch_size "
    "DETACH DELETE n "
    "RETURN count(*) AS deleted"
)
