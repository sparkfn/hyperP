"""Cypher query builder for variable-depth subgraph traversal."""

from __future__ import annotations

# Labels to exclude from traversal — too noisy for interactive exploration.
_EXCLUDED_LABELS = ("IngestRun", "SourceSystem")

_LABEL_FILTER = " AND ".join(f"NOT n:{label}" for label in _EXCLUDED_LABELS)

# Relationship types the traversal must never follow.
# FROM_SOURCE: SourceRecord -> SourceSystem fan-out to every record in that system.
# HAS_FACT: Person -> SourceRecord fan-out floods the graph at multi-hop depth.
# PART_OF_RUN: SourceRecord -> IngestRun -> SourceRecord fan-out across all
#   records in the same ingestion run.
_EXCLUDED_REL_TYPES = ("FROM_SOURCE", "HAS_FACT", "PART_OF_RUN")

_REL_FILTER = " AND ".join(f"type(r) <> '{rt}'" for rt in _EXCLUDED_REL_TYPES)

# Cap on total nodes returned to prevent browser overload and query blow-up.
_NODE_CAP = 200

# Shared query body: collect distinct neighbor nodes with a cap, then discover
# all relationships between the collected nodes.
_QUERY_BODY = """
CALL {{
  WITH start
  MATCH path = (start)-[*1..{max_hops}]-(n)
  WHERE {label_filter}
    AND ALL(r IN relationships(path) WHERE {rel_filter})
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
    "node_cap": _NODE_CAP,
}

# Pre-built queries for allowed depths (1 – 4).
_DEPTH_QUERIES: dict[int, str] = {
    depth: _PERSON_START + _QUERY_BODY.format(max_hops=depth, **_FMT)
    for depth in range(1, 5)
}

_NODE_DEPTH_QUERIES: dict[int, str] = {
    depth: _NODE_START + _QUERY_BODY.format(max_hops=depth, **_FMT)
    for depth in range(1, 5)
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
