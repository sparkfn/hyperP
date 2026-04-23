"""Cypher query builder for variable-depth subgraph traversal."""

from __future__ import annotations

# Labels to exclude from traversal — too noisy for interactive exploration.
_EXCLUDED_LABELS = ("IngestRun", "SourceSystem", "Entity")

_LABEL_FILTER = " AND ".join(f"NOT n:{label}" for label in _EXCLUDED_LABELS)

# Relationship types the traversal must never follow.
# FROM_SOURCE: SourceRecord -> SourceSystem fan-out.
# HAS_FACT: Person -> SourceRecord fan-out at multi-hop depth.
# PART_OF_RUN: SourceRecord -> IngestRun -> SourceRecord cross-run fan-out.
# SOLD_THROUGH: Order -> SourceSystem fan-out (all orders share the same SS).
# OPERATED_BY: Entity -> SourceSystem.
# SOLD_BY: Product -> Entity.
_EXCLUDED_REL_TYPES = (
    "FROM_SOURCE",
    "HAS_FACT",
    "PART_OF_RUN",
    "SOLD_THROUGH",
    "OPERATED_BY",
    "SOLD_BY",
)

_REL_FILTER = " AND ".join(f"type(r) <> '{rt}'" for rt in _EXCLUDED_REL_TYPES)

# Dead-end labels: these nodes can appear at the END of a path but must not
# be intermediate waypoints. This prevents fan-out through shared nodes
# (e.g. the same Product referenced by many LineItems across different Orders).
_DEAD_END_LABELS = ("Product",)

_DEAD_END_FILTER = (
    "NONE(mid IN nodes(path)[1..-1] WHERE "
    + " OR ".join(f"mid:{label}" for label in _DEAD_END_LABELS)
    + ")"
)

# Cap on total nodes returned to prevent browser overload and query blow-up.
_NODE_CAP = 200

_QUERY_BODY = """
CALL {{
  WITH start
  MATCH path = (start)-[*1..{max_hops}]-(n)
  WHERE {label_filter}
    AND ALL(r IN relationships(path) WHERE {rel_filter})
    AND {dead_end_filter}
  WITH start, collect(DISTINCT n)[0..{node_cap}] AS neighbors
  RETURN neighbors + [start] AS raw_nodes
}}
WITH raw_nodes
UNWIND raw_nodes AS node
WITH collect(DISTINCT node) AS unique_nodes
UNWIND unique_nodes AS a
WITH unique_nodes, a
UNWIND unique_nodes AS b
WITH unique_nodes, a, b
WHERE elementId(a) < elementId(b)
OPTIONAL MATCH (a)-[r]-(b)
WHERE r IS NOT NULL
WITH unique_nodes, collect(DISTINCT r) AS unique_rels
UNWIND unique_nodes AS node
WITH collect(DISTINCT {{
  id: elementId(node),
  label: head(labels(node)),
  properties: properties(node)
}}) AS nodes, unique_rels
UNWIND unique_rels AS rel
RETURN nodes,
       collect(DISTINCT {{
  id: elementId(rel),
  type: type(rel),
  source: elementId(startNode(rel)),
  target: elementId(endNode(rel)),
  properties: properties(rel)
}}) AS edges
"""

_PERSON_START = "MATCH (start:Person {person_id: $person_id})\n"
_NODE_START = "MATCH (start) WHERE elementId(start) = $element_id\n"

_FMT: dict[str, str | int] = {
    "label_filter": _LABEL_FILTER,
    "rel_filter": _REL_FILTER,
    "dead_end_filter": _DEAD_END_FILTER,
    "node_cap": _NODE_CAP,
}

# Pre-built queries for allowed depths (1 – 4).
_DEPTH_QUERIES: dict[int, str] = {
    depth: _PERSON_START + _QUERY_BODY.format(max_hops=depth, **_FMT) for depth in range(1, 5)
}

_NODE_DEPTH_QUERIES: dict[int, str] = {
    depth: _NODE_START + _QUERY_BODY.format(max_hops=depth, **_FMT) for depth in range(1, 5)
}

MIN_HOPS: int = 1
MAX_HOPS: int = 4
DEFAULT_HOPS: int = 2


def get_graph_query(max_hops: int) -> str:
    """Return the pre-built Cypher query for the given depth.

    Raises ``ValueError`` if *max_hops* is outside the allowed range.
    """
    if max_hops not in _DEPTH_QUERIES:
        raise ValueError(f"max_hops must be between {MIN_HOPS} and {MAX_HOPS}, got {max_hops}")
    return _DEPTH_QUERIES[max_hops]


def get_node_graph_query(max_hops: int) -> str:
    """Return the pre-built Cypher query for generic node traversal by elementId."""
    if max_hops not in _NODE_DEPTH_QUERIES:
        raise ValueError(f"max_hops must be between {MIN_HOPS} and {MAX_HOPS}, got {max_hops}")
    return _NODE_DEPTH_QUERIES[max_hops]
