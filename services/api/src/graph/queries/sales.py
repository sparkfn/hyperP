"""Cypher query for person sales history (Order → LineItem → Product)."""

from __future__ import annotations

GET_PERSON_SALES = """
MATCH (p:Person {person_id: $person_id})-[:PURCHASED]->(o:Order)
OPTIONAL MATCH (o)-[:SOLD_THROUGH]->(ss:SourceSystem)-[:OPERATED_BY]->(entity:Entity)
OPTIONAL MATCH (o)-[:CONTAINS]->(li:LineItem)-[:OF_PRODUCT]->(prod:Product)
WITH o, ss, entity, li, prod ORDER BY li.line_no
WITH o, ss, entity, collect(CASE WHEN li IS NOT NULL THEN {
  line_no: li.line_no,
  quantity: li.quantity,
  unit_price: li.unit_price,
  subtotal: li.subtotal,
  product_display_name: coalesce(prod.display_name, prod.name),
  product_sku: prod.sku,
  product_category: prod.category
} END) AS raw_items
RETURN o.order_no AS order_no,
       o.source_order_id AS source_order_id,
       o.ordered_at AS order_date,
       o.release_date AS release_date,
       o.total_amount AS total_amount,
       o.currency AS currency,
       ss.source_key AS source_system,
       coalesce(entity.display_name, entity.entity_key) AS entity_name,
       [x IN raw_items WHERE x IS NOT NULL] AS line_items
ORDER BY o.release_date DESC
SKIP $skip LIMIT $limit
"""

COUNT_PERSON_SALES = """
MATCH (p:Person {person_id: $person_id})-[:PURCHASED]->(o:Order)
RETURN count(o) AS total
"""
