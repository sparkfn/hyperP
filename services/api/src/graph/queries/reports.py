"""Cypher queries for the stretchy-reports feature.

Report definitions are stored as (:Report) nodes in Neo4j. Each node holds
the Cypher template, parameter schema (JSON string), and metadata.
"""

from __future__ import annotations

LIST_REPORTS: str = """
MATCH (r:Report)
RETURN r {
    .report_key, .display_name, .description, .category
} AS report
ORDER BY r.display_name
"""

GET_REPORT: str = """
MATCH (r:Report {report_key: $report_key})
RETURN r {
    .report_key, .display_name, .description, .category,
    .cypher_query, .parameters_json, .created_at, .updated_at
} AS report
"""

CREATE_REPORT: str = """
CREATE (r:Report {
    report_key: $report_key,
    display_name: $display_name,
    description: $description,
    category: $category,
    cypher_query: $cypher_query,
    parameters_json: $parameters_json,
    created_at: datetime(),
    updated_at: datetime()
})
RETURN r { .report_key } AS report
"""

UPDATE_REPORT: str = """
MATCH (r:Report {report_key: $report_key})
SET r.display_name = $display_name,
    r.description = $description,
    r.category = $category,
    r.cypher_query = $cypher_query,
    r.parameters_json = $parameters_json,
    r.updated_at = datetime()
RETURN r { .report_key } AS report
"""

DELETE_REPORT: str = """
MATCH (r:Report {report_key: $report_key})
DELETE r
RETURN count(*) AS deleted_count
"""

# -- Sample seed reports ------------------------------------------------------

SEED_REPORTS: list[dict[str, str]] = [
    {
        "report_key": "entity_person_summary",
        "display_name": "Entity Person Summary",
        "description": (
            "Lists all persons linked to a given entity with their status, "
            "source record count, and connection count."
        ),
        "category": "entities",
        "cypher_query": (
            "MATCH (e:Entity {entity_key: $entity_key})\n"
            "<-[:OPERATED_BY]-(ss:SourceSystem)<-[:FROM_SOURCE]-(sr:SourceRecord)\n"
            "<-[:HAS_FACT]-(p:Person)\n"
            "WHERE p.status = 'active'\n"
            "WITH DISTINCT p\n"
            "OPTIONAL MATCH (p)-[:HAS_FACT]->(src:SourceRecord)\n"
            "WITH p, count(DISTINCT src) AS source_count\n"
            "OPTIONAL MATCH (p)-[:IDENTIFIED_BY|LIVES_AT]-(shared)"
            "-[:IDENTIFIED_BY|LIVES_AT]-(other:Person)\n"
            "WHERE other.person_id <> p.person_id\n"
            "RETURN p.person_id AS person_id,\n"
            "       p.preferred_full_name AS name,\n"
            "       p.status AS status,\n"
            "       p.preferred_phone AS phone,\n"
            "       p.preferred_email AS email,\n"
            "       source_count,\n"
            "       count(DISTINCT other) AS connection_count\n"
            "ORDER BY name"
        ),
        "parameters_json": (
            '[{"name":"entity_key","label":"Entity Key",'
            '"param_type":"string","required":true,"default_value":null}]'
        ),
    },
    {
        "report_key": "persons_by_status",
        "display_name": "Persons by Status",
        "description": (
            "Counts persons grouped by status. Useful for monitoring "
            "merge activity and data health."
        ),
        "category": "analytics",
        "cypher_query": (
            "MATCH (p:Person)\n"
            "RETURN p.status AS status, count(p) AS person_count\n"
            "ORDER BY person_count DESC"
        ),
        "parameters_json": "[]",
    },
    {
        "report_key": "shared_phone_numbers",
        "display_name": "Shared Phone Numbers",
        "description": (
            "Finds phone identifiers shared by more than N persons. "
            "Helps identify potential data-quality issues or shared devices."
        ),
        "category": "data_quality",
        "cypher_query": (
            "MATCH (i:Identifier {identifier_type: 'phone'})"
            "<-[:IDENTIFIED_BY]-(p:Person)\n"
            "WHERE p.status = 'active'\n"
            "WITH i.normalized_value AS phone, collect(p.person_id) AS person_ids\n"
            "WHERE size(person_ids) >= $min_shared\n"
            "RETURN phone,\n"
            "       size(person_ids) AS shared_count,\n"
            "       person_ids[0..5] AS sample_person_ids\n"
            "ORDER BY shared_count DESC\n"
            "LIMIT 100"
        ),
        "parameters_json": (
            '[{"name":"min_shared","label":"Minimum Shared Count",'
            '"param_type":"integer","required":false,"default_value":"2"}]'
        ),
    },
    {
        "report_key": "top_buyers",
        "display_name": "Top Buyers by Order Count",
        "description": (
            "Lists active persons in descending order of the number of "
            "orders purchased. Includes total spend and latest order date."
        ),
        "category": "sales",
        "cypher_query": (
            "MATCH (p:Person)-[:PURCHASED]->(o:Order)\n"
            "WHERE p.status = 'active'\n"
            "WITH p,\n"
            "     count(o) AS order_count,\n"
            "     sum(o.total_amount) AS total_spend,\n"
            "     max(o.order_date) AS last_order_date\n"
            "RETURN p.person_id AS person_id,\n"
            "       p.preferred_full_name AS name,\n"
            "       p.preferred_phone AS phone,\n"
            "       p.preferred_email AS email,\n"
            "       order_count,\n"
            "       total_spend,\n"
            "       last_order_date\n"
            "ORDER BY order_count DESC\n"
            "LIMIT $limit"
        ),
        "parameters_json": (
            '[{"name":"limit","label":"Max Rows",'
            '"param_type":"integer","required":false,"default_value":"100"}]'
        ),
    },
]

SEED_REPORT_QUERY: str = """
MERGE (r:Report {report_key: $report_key})
ON CREATE SET
    r.display_name = $display_name,
    r.description = $description,
    r.category = $category,
    r.cypher_query = $cypher_query,
    r.parameters_json = $parameters_json,
    r.created_at = datetime(),
    r.updated_at = datetime()
ON MATCH SET
    r.display_name = $display_name,
    r.description = $description,
    r.category = $category,
    r.cypher_query = $cypher_query,
    r.parameters_json = $parameters_json,
    r.updated_at = datetime()
RETURN r { .report_key } AS report
"""
