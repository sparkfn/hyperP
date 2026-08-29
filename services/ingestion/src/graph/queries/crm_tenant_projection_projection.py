"""Bounded ledger-projection Cypher for immutable CRM tenant projection releases."""

from __future__ import annotations

READ_PROJECTION_INPUTS = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'projection'
})-[:HAS_PROJECTION_INPUT]->(input:CrmTenantProjectionInput)
WHERE release.projection_cursor_kind IS NULL
  OR CASE input.subject_kind WHEN 'contact' THEN 0 ELSE 1 END
     > CASE release.projection_cursor_kind WHEN 'contact' THEN 0 ELSE 1 END
  OR (input.subject_kind = release.projection_cursor_kind
    AND toInteger(input.subject_id) > release.projection_cursor_subject_id)
RETURN input.input_id AS input_id, input.subject_kind AS subject_kind,
  input.subject_id AS subject_id, input.snapshot_id AS snapshot_id
ORDER BY CASE input.subject_kind WHEN 'contact' THEN 0 ELSE 1 END, toInteger(input.subject_id)
LIMIT $page_limit
"""

READ_INPUT_SUPPORTS = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, mapping_revision_id: $mapping_revision_id,
  state: 'building', phase: 'projection'
})-[:MATERIALIZES_MAPPING_REVISION]->(revision:CrmTenantMappingRevision {
  revision_id: $mapping_revision_id, manifest_digest: release.mapping_manifest_digest,
  state: 'prepared'
})
MATCH (release)-[:HAS_PROJECTION_INPUT]->(input:CrmTenantProjectionInput {input_id: $input_id})
MATCH (input)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot:CrmCompanyMembershipSnapshot {
  snapshot_id: $snapshot_id, source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id
})
OPTIONAL MATCH (snapshot)-[snapshot_reference:HAS_MEMBERSHIP_OBSERVATION]->(
  observation:CrmCompanyMembershipObservation
)
WITH release, revision, input, snapshot, observation,
  count(snapshot_reference) AS snapshot_reference_count
OPTIONAL MATCH (:CrmCompanyMembershipSnapshot)-[owner_reference:HAS_MEMBERSHIP_OBSERVATION]->(
  observation
)
WITH release, revision, input, snapshot, observation, snapshot_reference_count,
  count(owner_reference) AS observation_owner_count
OPTIONAL MATCH (observation)-[:REFERENCES_COMPANY]->(company_reference:CrmCompanyReference)
WITH release, revision, input, snapshot, observation, snapshot_reference_count,
  observation_owner_count, company_reference,
  CASE WHEN observation IS NULL THEN 0 ELSE size([
    (observation)-[:REFERENCES_COMPANY]->() | 1
  ]) END AS company_reference_count
OPTIONAL MATCH (revision)-[:HAS_MAPPING_ENTRY]->(entry:CrmTenantMappingEntry {
  revision_id: $mapping_revision_id, company_id: observation.company_id
})-[:HAS_MAPPING_TARGET]->(target:CrmTenantMappingTarget)
  -[:TARGETS_ENTITY]->(entity:Entity)
RETURN snapshot.binding_count AS binding_count, snapshot.snapshot_digest AS snapshot_digest,
  snapshot.source_record_id AS snapshot_source_record_id,
  snapshot.source_record_pk AS snapshot_source_record_pk,
  snapshot.source_record_version AS snapshot_source_record_version,
  snapshot.source_record_hash AS snapshot_source_record_hash,
  CASE WHEN snapshot.observed_at IS NULL THEN NULL ELSE toString(snapshot.observed_at) END
    AS snapshot_observed_at,
  toString(snapshot.available_at) AS snapshot_available_at,
  snapshot.contract_version AS snapshot_contract_version,
  observation.observation_id AS observation_id,
  elementId(observation) AS observation_node_id, observation.snapshot_id AS observation_snapshot_id,
  observation.subject_kind AS observation_subject_kind,
  observation.subject_id AS observation_subject_id, observation.company_id AS company_id,
  observation.sort AS observation_sort, observation.role_id AS observation_role_id,
  observation.is_primary AS observation_is_primary,
  snapshot_reference_count AS snapshot_reference_count,
  observation_owner_count AS observation_owner_count,
  company_reference_count AS company_reference_count,
  company_reference.company_id AS reference_company_id,
  company_reference.source_key AS reference_source_key,
  company_reference.source_instance_id AS reference_source_instance_id,
  company_reference.control_instance_id AS reference_control_instance_id,
  target.target_id AS mapping_target_id,
  target.entity_key AS entity_key, target.relationship_kind AS relationship_kind
ORDER BY observation.observation_id, target.target_id
"""

WRITE_ASSOCIATIONS = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'projection'
})-[:MATERIALIZES_MAPPING_REVISION]->(revision:CrmTenantMappingRevision {
  revision_id: release.mapping_revision_id, manifest_digest: release.mapping_manifest_digest,
  state: 'prepared'
})
MATCH (release)-[:HAS_PROJECTION_INPUT]->(input:CrmTenantProjectionInput {input_id: $input_id})
MATCH (input)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot:CrmCompanyMembershipSnapshot)
UNWIND $supports AS item
MATCH (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation:CrmCompanyMembershipObservation {
  observation_id: item.observation_id
})
MATCH (revision)-[:HAS_MAPPING_ENTRY]->(:CrmTenantMappingEntry {
  revision_id: release.mapping_revision_id, company_id: observation.company_id
})-[:HAS_MAPPING_TARGET]->(target:CrmTenantMappingTarget {
  target_id: item.mapping_target_id, entity_key: item.entity_key,
  relationship_kind: item.relationship_kind
})-[:TARGETS_ENTITY]->(entity:Entity {entity_key: item.entity_key})
MERGE (association:CrmTenantProjectionAssociation {
  release_id: $release_id, subject_kind: item.subject_kind, subject_id: item.subject_id,
  entity_key: item.entity_key, relationship_kind: item.relationship_kind
})
ON CREATE SET association.association_id = item.association_id, association.input_id = $input_id,
  association.created_at = datetime()
WITH release, input, entity, observation, target, association, item
WHERE association.association_id = item.association_id AND association.input_id = $input_id
MERGE (input)-[:HAS_PROJECTION_ASSOCIATION]->(association)
MERGE (association)-[:TARGETS_ENTITY]->(entity)
MERGE (support:CrmTenantProjectionSupport {
  association_id: item.association_id, membership_observation_id: item.observation_id,
  mapping_target_id: item.mapping_target_id
})
ON CREATE SET support.support_id = item.support_id, support.support_digest = item.support_digest,
  support.release_id = $release_id,
  support.created_at = datetime()
WITH association, observation, target, support, item
WHERE support.support_id = item.support_id AND support.support_digest = item.support_digest
  AND support.release_id = $release_id
MERGE (association)-[:HAS_PROJECTION_SUPPORT]->(support)
MERGE (support)-[:SUPPORTED_BY_MEMBERSHIP]->(observation)
MERGE (support)-[:SUPPORTED_BY_MAPPING_TARGET]->(target)
RETURN count(DISTINCT association) AS associations, count(DISTINCT support) AS supports
"""

WRITE_DECISION = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'projection'
})-[:HAS_PROJECTION_INPUT]->(input:CrmTenantProjectionInput {input_id: $input_id})
OPTIONAL MATCH (input)-[:HAS_PROJECTION_ASSOCIATION]->(association:CrmTenantProjectionAssociation)
WITH input, count(DISTINCT association) AS association_count
WHERE ($decision = 'associated' AND association_count > 0)
  OR ($decision = 'zero_target' AND association_count = 0)
MERGE (decision:CrmTenantProjectionDecision {release_id: $release_id, input_id: $input_id})
ON CREATE SET decision.decision = $decision, decision.zero_target_reason = $zero_target_reason,
  decision.decision_digest = $decision_digest, decision.created_at = datetime()
WITH input, decision
WHERE decision.decision = $decision AND decision.decision_digest = $decision_digest
  AND ((decision.zero_target_reason IS NULL AND $zero_target_reason IS NULL)
    OR decision.zero_target_reason = $zero_target_reason)
MERGE (input)-[:HAS_PROJECTION_DECISION]->(decision)
RETURN decision.input_id AS input_id
"""

ADVANCE_PROJECTION = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'projection'
})
WHERE release.decision_count = $prior_decision_count
  AND release.association_count = $prior_association_count
  AND release.support_count = $prior_support_count
SET release.projection_cursor_kind = $cursor_kind,
  release.projection_cursor_subject_id = $cursor_subject_id,
  release.decision_count = $decision_count,
  release.association_count = $association_count,
  release.support_count = $support_count,
  release.phase = CASE WHEN $done THEN 'complete' ELSE 'projection' END,
  release.projection_complete = $done, release.updated_at = datetime()
RETURN properties(release) AS release
"""
