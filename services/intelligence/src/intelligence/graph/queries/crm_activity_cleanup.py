"""Parameterized exact-identity graph queries for CRM activity cleanup.

No query performs a population scan, schema operation, dynamic relationship-type
injection, or ``DETACH DELETE``.  Element IDs are only opaque parameters returned
by the inspection query in the same database identity scope.
"""

from __future__ import annotations

DATABASE_IDENTITY = """
CALL db.info() YIELD id
RETURN toString(id) AS database_identity
"""

# The first OPTIONAL MATCH deliberately has no label/type/source predicate: a
# manifest PK must reveal a wrong-label, duplicate, or wrong-source replacement,
# rather than disappear behind an admission filter.
INSPECT_IDENTITIES = """
UNWIND $source_record_pks AS source_record_pk
OPTIONAL MATCH (candidate {source_record_pk: source_record_pk})
WITH source_record_pk, [node IN collect(candidate) WHERE node IS NOT NULL] AS candidates
WITH source_record_pk, candidates, head(candidates) AS record
CALL (record) {
  WITH record
  OPTIONAL MATCH (record)-[relationship]-()
  RETURN count(relationship) AS incident_relationship_count
}
CALL (record) {
  WITH record
  OPTIONAL MATCH (record)-[relationship]-(other)
  WITH record, relationship, other,
       CASE
         WHEN relationship IS NULL THEN ''
         WHEN startNode(relationship) = record AND endNode(relationship) = record THEN 'self'
         WHEN startNode(relationship) = record THEN 'outbound'
         ELSE 'inbound'
       END AS direction
  ORDER BY type(relationship), direction, labels(other),
           coalesce(toString(other.source_record_pk), ''),
           coalesce(toString(other.person_id), ''),
           coalesce(toString(other.identifier_type), ''),
           elementId(other),
           coalesce(toString(other.source_key), ''),
           coalesce(toString(other.review_case_id), ''),
           coalesce(toString(other.match_decision_id), ''), elementId(relationship)
  LIMIT $relationship_limit_plus_one
  RETURN [item IN collect(CASE WHEN relationship IS NULL THEN null ELSE {
    relationship_element_id: elementId(relationship),
    direction: direction,
    relationship_type: type(relationship),
    other_element_id: elementId(other),
    other_labels: labels(other),
    other_source_record_pk: other.source_record_pk,
    other_person_id: other.person_id,
    other_identifier_type: other.identifier_type,
    other_identifier_comparison_token: CASE
      WHEN 'Identifier' IN labels(other) THEN elementId(other) ELSE null
    END,
    other_source_key: other.source_key,
    other_review_case_id: other.review_case_id,
    other_match_decision_id: other.match_decision_id
  } END) WHERE item IS NOT NULL] AS incident_relationships
}
RETURN source_record_pk,
       size(candidates) AS matching_node_count,
       CASE WHEN record IS NULL THEN null ELSE elementId(record) END AS record_element_id,
       CASE WHEN record IS NULL THEN [] ELSE labels(record) END AS labels,
       record.record_type AS record_type,
       record.source_instance_id AS source_instance_id,
       record.source_record_id AS source_record_id,
       toString(record.source_record_version) AS source_record_version,
       record.source_version_key AS source_version_key,
       record.record_hash AS record_hash,
       record.lifecycle_status AS lifecycle_status,
       record.history_family AS history_family,
       record.history_source AS history_source,
       record.projection_source AS projection_source,
       toString(record.projection_version) AS projection_version,
       record.parent_source_record_pk AS parent_source_record_pk,
       record.parent_source_instance_id AS parent_source_instance_id,
       record.parent_source_record_id AS parent_source_record_id,
       record.parent_record_type AS parent_record_type,
       record.parent_source_system AS parent_source_system,
       incident_relationship_count,
       incident_relationships
ORDER BY source_record_pk
"""

# A protected relationship can be proved unchanged by its opaque relationship
# element ID plus exact type, direction, and endpoint identity.  This query is
# intentionally a finite lookup over evidence captured during planning.
VERIFY_PROTECTED = """
UNWIND $protected_relationships AS expected
OPTIONAL MATCH (selected)-[relationship]-(other)
WHERE elementId(relationship) = expected.relationship_element_id
  AND selected.source_record_pk = expected.selected_source_record_pk
WITH expected, selected, relationship, other,
     CASE
       WHEN relationship IS NULL THEN null
       WHEN startNode(relationship) = selected AND endNode(relationship) = selected THEN 'self'
       WHEN startNode(relationship) = selected THEN 'outbound'
       ELSE 'inbound'
     END AS direction
RETURN expected.selected_source_record_pk AS selected_source_record_pk,
       expected.relationship_element_id AS relationship_element_id,
       CASE WHEN relationship IS NULL THEN null ELSE type(relationship) END AS relationship_type,
       direction,
       CASE WHEN other IS NULL THEN null ELSE elementId(other) END AS other_element_id,
       CASE WHEN other IS NULL THEN [] ELSE labels(other) END AS other_labels,
       other.source_record_pk AS other_source_record_pk,
       other.person_id AS other_person_id,
       other.identifier_type AS other_identifier_type,
       CASE WHEN 'Identifier' IN labels(other) THEN elementId(other) ELSE null END
         AS other_identifier_comparison_token,
       other.source_key AS other_source_key,
       other.review_case_id AS other_review_case_id,
       other.match_decision_id AS other_match_decision_id
ORDER BY selected_source_record_pk, relationship_element_id
"""

# SETting a node property to its own value obtains a Neo4j write lock without
# changing the source fact. The transaction retains that lock through exact
# reinspection and the enumerated relationship/node deletions below.
LOCK_IDENTITIES_FOR_REVALIDATION = """
UNWIND $source_record_pks AS source_record_pk
MATCH (record {source_record_pk: source_record_pk})
SET record.source_record_pk = record.source_record_pk
RETURN record.source_record_pk AS source_record_pk
ORDER BY source_record_pk
"""

# Source-system locks serialize a concurrent CREATE of a future selected
# SourceRecord and its FROM_SOURCE relationship with absence revalidation.
LOCK_SOURCE_SYSTEMS_FOR_REVALIDATION = """
UNWIND $source_keys AS source_key
OPTIONAL MATCH (source:SourceSystem {source_key: source_key})
WITH source_key, [item IN collect(source) WHERE item IS NOT NULL] AS sources
FOREACH (source IN sources | SET source.source_key = source.source_key)
RETURN source_key, size(sources) AS source_system_count
ORDER BY source_key
"""

DELETE_RELATIONSHIPS_BY_OWNERS = """
UNWIND $relationship_owners AS owner
MATCH (record)
WHERE elementId(record) = owner.record_element_id
UNWIND owner.relationship_element_ids AS relationship_element_id
MATCH (record)-[relationship]-()
WHERE elementId(relationship) = relationship_element_id
DELETE relationship
RETURN count(*) AS deleted_count
"""

DELETE_NODES_BY_ELEMENT_IDS = """
UNWIND $record_element_ids AS record_element_id
MATCH (record)
WHERE elementId(record) = record_element_id
DELETE record
RETURN count(*) AS deleted_count
"""
