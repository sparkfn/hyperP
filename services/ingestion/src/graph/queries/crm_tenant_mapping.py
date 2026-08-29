"""Parameterized Cypher for immutable CRM tenant mapping authority (#304)."""

from __future__ import annotations

READ_REVISION = """
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: $revision_id})
WHERE revision.manifest_digest = $manifest_digest
OPTIONAL MATCH (revision)-[entry_link:HAS_MAPPING_ENTRY]->(entry:CrmTenantMappingEntry)
OPTIONAL MATCH (entry)-[target_link:HAS_MAPPING_TARGET]->(target:CrmTenantMappingTarget)
OPTIONAL MATCH (target)-[entity_link:TARGETS_ENTITY]->(entity)
RETURN properties(revision) AS revision, properties(entry) AS entry, properties(target) AS target,
       entity.entity_key AS entity_key, labels(entity) AS entity_labels
ORDER BY entry.company_id, target.entity_key, target.relationship_kind
"""

READ_BY_REQUEST = """
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    preparation_request_id: $preparation_request_id})
RETURN revision.revision_id AS revision_id, revision.manifest_digest AS manifest_digest
"""

READ_BY_ID = """
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: $revision_id})
RETURN revision.revision_id AS revision_id, revision.manifest_digest AS manifest_digest
"""

READ_ACTIVE_HEAD = """
MATCH (head:CrmTenantMappingActiveHead {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
RETURN properties(head) AS head
"""

VALIDATE_SOURCE_SYNC_AT_LINEARIZATION = """
MATCH (head:CrmTenantMappingActiveHead {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    head_id: $head_id, active_manifest_digest: $mapping_head_digest})
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: head.active_revision_id, manifest_digest: head.active_manifest_digest})
WHERE revision.state = 'active' AND revision.revision_number = head.active_revision_number
RETURN revision.revision_id AS revision_id
"""

VALIDATE_MAPPING_PREPARE_AT_LINEARIZATION = """
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: $revision_id, manifest_digest: $manifest_digest})
WHERE revision.state = 'prepared'
  AND revision.expected_head_id = $expected_head_id
  AND revision.expected_head_present = $expected_head_present
  AND (
      ($expected_head_present = false
       AND revision.expected_active_revision_id IS NULL
       AND revision.expected_active_revision_number IS NULL
       AND revision.expected_active_manifest_digest IS NULL)
      OR
      ($expected_head_present = true
       AND revision.expected_active_revision_id = $expected_active_revision_id
       AND revision.expected_active_revision_number = $expected_active_revision_number
       AND revision.expected_active_manifest_digest = $expected_active_manifest_digest)
  )
OPTIONAL MATCH (head:CrmTenantMappingActiveHead {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    head_id: $expected_head_id})
WITH revision, head
WHERE ($expected_head_present = false AND head IS NULL)
   OR ($expected_head_present = true
       AND head.active_revision_id = $expected_active_revision_id
       AND head.active_revision_number = $expected_active_revision_number
       AND head.active_manifest_digest = $expected_active_manifest_digest)
RETURN revision.revision_id AS revision_id
"""

VALIDATE_MAPPING_ROLLBACK_AT_LINEARIZATION = """
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: $revision_id, manifest_digest: $manifest_digest})
MATCH (historical:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: $rollback_of_revision_id, manifest_digest: $rollback_of_manifest_digest})
WHERE revision.state = 'prepared'
  AND revision.rollback_of_revision_id = $rollback_of_revision_id
  AND revision.rollback_of_revision_number = $rollback_of_revision_number
  AND revision.rollback_of_manifest_digest = $rollback_of_manifest_digest
  AND historical.state IN ['active', 'superseded']
  AND historical.revision_number = $rollback_of_revision_number
  AND revision.expected_head_id = $expected_head_id
  AND revision.expected_head_present = true
  AND revision.expected_active_revision_id = $expected_active_revision_id
  AND revision.expected_active_revision_number = $expected_active_revision_number
  AND revision.expected_active_manifest_digest = $expected_active_manifest_digest
  AND historical.revision_number < $expected_active_revision_number
MATCH (head:CrmTenantMappingActiveHead {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    head_id: $expected_head_id, active_revision_id: $expected_active_revision_id,
    active_revision_number: $expected_active_revision_number,
    active_manifest_digest: $expected_active_manifest_digest})
RETURN revision.revision_id AS revision_id
"""

VALIDATE_ENTITIES = """
UNWIND $entity_keys AS entity_key
OPTIONAL MATCH (entity:Entity {entity_key: entity_key})
RETURN entity_key, count(entity) AS entity_count
ORDER BY entity_key
"""

ALLOCATE_REVISION_NUMBER = """
MATCH (counter:CrmTenantMappingScopeCounter {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
SET counter.next_revision_number = counter.next_revision_number + 1
RETURN counter.next_revision_number AS revision_number
"""

LOCK_SCOPE = """
MERGE (counter:CrmTenantMappingScopeCounter {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id})
ON CREATE SET counter.next_revision_number = 0, counter.serialization_version = 0
SET counter.serialization_version = coalesce(counter.serialization_version, 0) + 1
RETURN counter.serialization_version AS serialization_version
"""

CHECK_REVISION_ID = """
OPTIONAL MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id})
RETURN count(revision) AS revision_count
"""

CREATE_REVISION = """
CREATE (revision:CrmTenantMappingRevision $revision_properties)
RETURN revision.revision_id AS revision_id
"""

CREATE_ENTRIES = """
MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id})
UNWIND $entries AS item
CREATE (entry:CrmTenantMappingEntry {revision_id: $revision_id, entry_id: item.entry_id,
    company_id: item.company_id})
CREATE (revision)-[:HAS_MAPPING_ENTRY]->(entry)
RETURN count(entry) AS entry_count
"""

CREATE_TARGETS = """
UNWIND $targets AS item
MATCH (entry:CrmTenantMappingEntry {entry_id: item.entry_id})
MATCH (entity:Entity {entity_key: item.entity_key})
CREATE (target:CrmTenantMappingTarget {entry_id: item.entry_id, target_id: item.target_id,
    entity_key: item.entity_key, relationship_kind: item.relationship_kind})
CREATE (entry)-[:HAS_MAPPING_TARGET]->(target)
CREATE (target)-[:TARGETS_ENTITY]->(entity)
RETURN count(target) AS target_count
"""

REJECT_REVISION = """
MATCH (revision:CrmTenantMappingRevision {source_key: $source_key,
    source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
    revision_id: $revision_id, manifest_digest: $manifest_digest})
WHERE revision.state = 'prepared'
SET revision.state = 'rejected', revision.rejection_actor = $rejection_actor,
    revision.rejection_reference = $rejection_reference, revision.rejection_reason = $rejection_reason,
    revision.rejected_at = $rejected_at, revision.rejection_authorization_actor = $authorization_actor,
    revision.rejection_authorization_reference = $authorization_reference,
    revision.rejection_authorization_digest = $authorization_digest,
    revision.rejection_authorized_at = $authorized_at,
    revision.rejection_authorization_expires_at = $authorization_expires_at,
    revision.rejection_request_fingerprint = $rejection_request_fingerprint
RETURN revision.revision_id AS revision_id
"""

READ_TOPOLOGY_VIOLATIONS = """
MATCH (revision:CrmTenantMappingRevision {revision_id: $revision_id})
CALL {
    WITH revision
    OPTIONAL MATCH (revision)-[link]->(child)
    RETURN count(CASE
        WHEN link IS NULL THEN NULL
        WHEN type(link) = 'HAS_MAPPING_ENTRY' AND child:CrmTenantMappingEntry THEN NULL
        ELSE link
    END) AS bad_revision_links
}
CALL {
    WITH revision
    OPTIONAL MATCH (revision)-[:HAS_MAPPING_ENTRY]->(entry)-[link]->(child)
    RETURN count(CASE
        WHEN link IS NULL THEN NULL
        WHEN type(link) = 'HAS_MAPPING_TARGET' AND child:CrmTenantMappingTarget THEN NULL
        ELSE link
    END) AS bad_entry_links
}
CALL {
    WITH revision
    OPTIONAL MATCH (revision)-[:HAS_MAPPING_ENTRY]->(:CrmTenantMappingEntry)
        -[:HAS_MAPPING_TARGET]->(target)-[link]->(child)
    RETURN count(CASE
        WHEN link IS NULL THEN NULL
        WHEN type(link) = 'TARGETS_ENTITY' AND child:Entity THEN NULL
        ELSE link
    END) AS bad_target_links
}
CALL {
    WITH revision
    OPTIONAL MATCH (entry:CrmTenantMappingEntry {revision_id: revision.revision_id})
    OPTIONAL MATCH (revision)-[link:HAS_MAPPING_ENTRY]->(entry)
    RETURN count(CASE
        WHEN entry IS NULL THEN NULL
        WHEN link IS NULL THEN entry
        ELSE NULL
    END) AS orphan_entries
}
CALL {
    WITH revision
    OPTIONAL MATCH (entry:CrmTenantMappingEntry {revision_id: revision.revision_id})
    WITH revision, [entry_id IN collect(entry.entry_id) WHERE entry_id IS NOT NULL] AS entry_ids
    OPTIONAL MATCH (target:CrmTenantMappingTarget)
    WHERE target.entry_id IN entry_ids
    OPTIONAL MATCH (owner:CrmTenantMappingEntry {revision_id: revision.revision_id,
        entry_id: target.entry_id})-[link:HAS_MAPPING_TARGET]->(target)
    RETURN count(CASE
        WHEN target IS NULL THEN NULL
        WHEN link IS NULL THEN target
        ELSE NULL
    END) AS orphan_targets
}
CALL {
    WITH revision
    OPTIONAL MATCH (entry:CrmTenantMappingEntry {revision_id: revision.revision_id})
    OPTIONAL MATCH (owner)-[link:HAS_MAPPING_ENTRY]->(entry)
    WITH revision, entry, collect(owner) AS owners, count(link) AS owner_count
    RETURN count(CASE
        WHEN entry IS NULL THEN NULL
        WHEN owner_count = 1 AND single(owner IN owners WHERE owner = revision) THEN NULL
        ELSE entry
    END) AS bad_entry_owners
}
CALL {
    WITH revision
    OPTIONAL MATCH (entry:CrmTenantMappingEntry {revision_id: revision.revision_id})
    OPTIONAL MATCH (target:CrmTenantMappingTarget {entry_id: entry.entry_id})
    OPTIONAL MATCH (owner)-[link:HAS_MAPPING_TARGET]->(target)
    WITH entry, target, collect(owner) AS owners, count(link) AS owner_count
    RETURN count(CASE
        WHEN target IS NULL THEN NULL
        WHEN owner_count = 1 AND single(owner IN owners WHERE owner = entry) THEN NULL
        ELSE target
    END) AS bad_target_owners
}
RETURN bad_revision_links, bad_entry_links, bad_target_links, orphan_entries, orphan_targets,
       bad_entry_owners, bad_target_owners
"""
