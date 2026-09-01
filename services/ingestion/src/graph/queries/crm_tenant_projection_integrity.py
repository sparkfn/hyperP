"""Terminal transition and strict completed-reader Cypher for CRM projections."""

from __future__ import annotations

COMPLETE_RELEASE = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'complete', capture_complete: true, projection_complete: true,
  contract_version: $contract_version
})-[:MATERIALIZES_SOURCE_CENSUS]->(census:StandaloneCrmCensus {
  census_id: release.source_census_id, fingerprint: release.source_census_fingerprint,
  source_key: release.source_key, source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, census_kind: 'source_sync', status: 'completed'
})
MATCH (contact:StandaloneCrmCensusUnit {census_id: census.census_id, stream_kind: 'contact'})
MATCH (lead:StandaloneCrmCensusUnit {census_id: census.census_id, stream_kind: 'lead'})
OPTIONAL MATCH (contact_checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: census.census_id, stream_kind: 'contact'
})
OPTIONAL MATCH (lead_checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: census.census_id, stream_kind: 'lead'
})
MATCH (release)-[:MATERIALIZES_MAPPING_REVISION]->(revision:CrmTenantMappingRevision {
  source_key: release.source_key, source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, revision_id: release.mapping_revision_id,
  revision_number: release.mapping_revision_number, manifest_digest: release.mapping_manifest_digest,
  state: 'prepared', expected_head_id: release.expected_mapping_head_id,
  expected_head_present: release.expected_mapping_head_present
})
OPTIONAL MATCH (mapping_head:CrmTenantMappingActiveHead {
  source_key: release.source_key, source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, head_id: release.expected_mapping_head_id
})
OPTIONAL MATCH (projection_head:CrmTenantProjectionActiveHead {
  source_key: release.source_key, source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, head_id: release.projection_head_id
})
WITH DISTINCT release, census, contact, lead, contact_checkpoint, lead_checkpoint, revision,
  mapping_head, projection_head
WHERE contact.generation = release.contact_unit_generation
  AND lead.generation = release.lead_unit_generation
  AND contact.state = release.contact_unit_state AND lead.state = release.lead_unit_state
  AND contact.frozen_upper_id = release.contact_frozen_upper_id
  AND lead.frozen_upper_id = release.lead_frozen_upper_id
  AND ((contact.state = 'completed'
      AND release.contact_checkpoint_present = true
      AND NOT EXISTS { MATCH (other:StandaloneCrmCensusCheckpoint {
        census_id: census.census_id, stream_kind: 'contact'
      }) WHERE id(other) <> id(contact_checkpoint) }
      AND contact_checkpoint.generation = release.contact_checkpoint_generation
      AND contact_checkpoint.frozen_upper_id = release.contact_frozen_upper_id
      AND contact_checkpoint.last_committed_id = release.contact_frozen_upper_id
      AND contact_checkpoint.processed_rows = release.contact_processed_rows
      AND contact_checkpoint.skipped_rows = release.contact_skipped_rows)
    OR (contact.state = 'no_work' AND release.contact_checkpoint_present = false
      AND release.contact_unit_generation > 0 AND release.contact_frozen_upper_id = 0
      AND release.contact_processed_rows = 0 AND release.contact_skipped_rows = 0
      AND contact_checkpoint IS NULL))
  AND ((lead.state = 'completed'
      AND release.lead_checkpoint_present = true
      AND NOT EXISTS { MATCH (other:StandaloneCrmCensusCheckpoint {
        census_id: census.census_id, stream_kind: 'lead'
      }) WHERE id(other) <> id(lead_checkpoint) }
      AND lead_checkpoint.generation = release.lead_checkpoint_generation
      AND lead_checkpoint.frozen_upper_id = release.lead_frozen_upper_id
      AND lead_checkpoint.last_committed_id = release.lead_frozen_upper_id
      AND lead_checkpoint.processed_rows = release.lead_processed_rows
      AND lead_checkpoint.skipped_rows = release.lead_skipped_rows)
    OR (lead.state = 'no_work' AND release.lead_checkpoint_present = false
      AND release.lead_unit_generation > 0 AND release.lead_frozen_upper_id = 0
      AND release.lead_processed_rows = 0 AND release.lead_skipped_rows = 0
      AND lead_checkpoint IS NULL))
  AND ((release.expected_mapping_head_present = false
      AND revision.expected_active_revision_id IS NULL
      AND revision.expected_active_revision_number IS NULL
      AND revision.expected_active_manifest_digest IS NULL
      AND mapping_head IS NULL)
    OR (release.expected_mapping_head_present = true
      AND revision.expected_active_revision_id = release.expected_mapping_active_revision_id
      AND revision.expected_active_revision_number = release.expected_mapping_active_revision_number
      AND revision.expected_active_manifest_digest = release.expected_mapping_head_digest
      AND mapping_head.active_revision_id = release.expected_mapping_active_revision_id
      AND mapping_head.active_revision_number = release.expected_mapping_active_revision_number
      AND mapping_head.active_manifest_digest = release.expected_mapping_head_digest))
  AND ((release.expected_prior_head_present = false AND projection_head IS NULL)
    OR (release.expected_prior_head_present = true
      AND release.expected_prior_head_id = release.projection_head_id
      AND projection_head.active_release_id = release.expected_prior_release_id
      AND projection_head.active_release_number = release.expected_prior_release_number
      AND projection_head.active_release_fingerprint = release.expected_prior_release_fingerprint
      AND release.release_number > release.expected_prior_release_number))
CALL {
  WITH release
  OPTIONAL MATCH (input:CrmTenantProjectionInput {release_id: release.release_id})
  RETURN count(DISTINCT input) AS actual_input_count
}
CALL {
  WITH release
  OPTIONAL MATCH (decision:CrmTenantProjectionDecision {release_id: release.release_id})
  RETURN count(DISTINCT decision) AS actual_decision_count
}
CALL {
  WITH release
  OPTIONAL MATCH (association:CrmTenantProjectionAssociation {release_id: release.release_id})
  RETURN count(DISTINCT association) AS actual_association_count
}
CALL {
  WITH release
  OPTIONAL MATCH (support:CrmTenantProjectionSupport {release_id: release.release_id})
  RETURN count(DISTINCT support) AS actual_support_count
}
WITH release, actual_input_count, actual_decision_count, actual_association_count,
  actual_support_count
WHERE actual_input_count = release.input_count
  AND actual_decision_count = release.decision_count
  AND actual_association_count = release.association_count
  AND actual_support_count = release.support_count
  AND actual_input_count = actual_decision_count
  AND NOT EXISTS {
    MATCH (release)-[:HAS_PROJECTION_INPUT]->(owned_input:CrmTenantProjectionInput)
    WHERE owned_input.release_id <> release.release_id
    RETURN owned_input
  }
  AND NOT EXISTS {
    MATCH (input:CrmTenantProjectionInput {release_id: release.release_id})
    OPTIONAL MATCH (owner)-[owner_link:HAS_PROJECTION_INPUT]->(input)
    OPTIONAL MATCH (input)-[snapshot_link:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
    OPTIONAL MATCH (input)-[decision_link:HAS_PROJECTION_DECISION]->(decision)
    WITH release, input, count(DISTINCT owner_link) AS owner_links,
      count(DISTINCT owner) AS owner_nodes,
      count(DISTINCT CASE WHEN owner:CrmTenantProjectionRelease
        AND owner.release_id = release.release_id THEN owner END) AS current_release_owners,
      count(DISTINCT snapshot_link) AS snapshot_links, count(DISTINCT snapshot) AS snapshots,
      count(DISTINCT decision_link) AS decisions, collect(DISTINCT snapshot.snapshot_id) AS snapshot_ids,
      collect(DISTINCT snapshot.binding_count) AS snapshot_binding_counts,
      collect(DISTINCT snapshot.snapshot_digest) AS snapshot_digests,
      collect(DISTINCT snapshot.subject_kind) AS snapshot_subject_kinds,
      collect(DISTINCT snapshot.subject_id) AS snapshot_subject_ids
    WHERE owner_links <> 1 OR owner_nodes <> 1 OR current_release_owners <> 1
      OR snapshot_links <> 1 OR snapshots <> 1 OR decisions <> 1
      OR snapshot_ids <> [input.snapshot_id] OR size(snapshot_binding_counts) <> 1
      OR snapshot_digests <> [input.snapshot_digest]
      OR snapshot_subject_kinds <> [input.subject_kind] OR snapshot_subject_ids <> [input.subject_id]
      OR snapshot_binding_counts[0] IS NULL OR snapshot_binding_counts[0] < 0
      OR size(snapshot_digests) <> 1 OR snapshot_digests[0] IS NULL
      OR size(snapshot_digests[0]) <> 71 OR NOT (snapshot_digests[0] STARTS WITH 'sha256:')
      OR input.subject_kind IS NULL OR NOT (input.subject_kind IN ['contact', 'lead'])
      OR input.subject_id IS NULL OR input.input_digest IS NULL OR input.snapshot_digest IS NULL
      OR size(input.input_digest) <> 71 OR NOT (input.input_digest STARTS WITH 'sha256:')
      OR size(input.snapshot_digest) <> 71 OR NOT (input.snapshot_digest STARTS WITH 'sha256:')
    RETURN input
  }
  AND NOT EXISTS {
    MATCH (decision:CrmTenantProjectionDecision {release_id: release.release_id})
    OPTIONAL MATCH (input)-[owner:HAS_PROJECTION_DECISION]->(decision)
    OPTIONAL MATCH (input)-[:HAS_PROJECTION_ASSOCIATION]->(association)
    OPTIONAL MATCH (input)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
    WITH release, decision, count(DISTINCT owner) AS owners, count(DISTINCT input) AS inputs,
      count(DISTINCT association) AS associations, collect(DISTINCT input.release_id) AS input_release_ids,
      collect(DISTINCT input.input_id) AS input_ids,
      collect(DISTINCT association.release_id) AS association_release_ids,
      collect(DISTINCT snapshot.binding_count) AS snapshot_binding_counts
    WHERE owners <> 1 OR inputs <> 1
      OR input_release_ids <> [release.release_id] OR input_ids <> [decision.input_id]
      OR decision.decision IS NULL OR NOT (decision.decision IN ['associated', 'zero_target'])
      OR decision.decision_digest IS NULL OR size(decision.decision_digest) <> 71
      OR NOT (decision.decision_digest STARTS WITH 'sha256:')
      OR (decision.decision = 'associated'
        AND (associations = 0 OR association_release_ids <> [release.release_id]
          OR decision.zero_target_reason IS NOT NULL))
      OR (decision.decision = 'zero_target' AND (associations <> 0
        OR decision.zero_target_reason IS NULL
        OR NOT (decision.zero_target_reason IN ['empty_membership', 'no_mapped_targets'])
        OR (decision.zero_target_reason = 'empty_membership'
          AND snapshot_binding_counts <> [0])
        OR (decision.zero_target_reason = 'no_mapped_targets'
          AND (snapshot_binding_counts = [0] OR size(snapshot_binding_counts) <> 1))))
    RETURN decision
  }
  AND NOT EXISTS {
    MATCH (association:CrmTenantProjectionAssociation {release_id: release.release_id})
    OPTIONAL MATCH (input)-[input_link:HAS_PROJECTION_ASSOCIATION]->(association)
    OPTIONAL MATCH (association)-[entity_link:TARGETS_ENTITY]->(entity:Entity)
    OPTIONAL MATCH (association)-[support_link:HAS_PROJECTION_SUPPORT]->(support)
    WITH release, association, count(DISTINCT input_link) AS inputs,
      count(DISTINCT input) AS input_nodes, count(DISTINCT entity_link) AS entities,
      count(DISTINCT entity) AS entity_nodes, count(DISTINCT support_link) AS supports,
      collect(DISTINCT input.release_id) AS input_release_ids,
      collect(DISTINCT input.input_id) AS input_ids,
      collect(DISTINCT input.subject_kind) AS input_subject_kinds,
      collect(DISTINCT input.subject_id) AS input_subject_ids,
      collect(DISTINCT entity.entity_key) AS entity_keys,
      collect(DISTINCT support.release_id) AS support_release_ids
    WHERE inputs <> 1 OR input_nodes <> 1 OR entities <> 1 OR entity_nodes <> 1 OR supports = 0
      OR input_release_ids <> [release.release_id] OR input_ids <> [association.input_id]
      OR input_subject_kinds <> [association.subject_kind]
      OR input_subject_ids <> [association.subject_id]
      OR association.relationship_kind IS NULL OR association.relationship_kind <> 'tenant_member'
      OR association.association_id IS NULL OR size(association.association_id) <> 71
      OR NOT (association.association_id STARTS WITH 'sha256:')
      OR entity_keys <> [association.entity_key] OR support_release_ids <> [release.release_id]
    RETURN association
  }
  AND NOT EXISTS {
    MATCH (support:CrmTenantProjectionSupport {release_id: release.release_id})
    OPTIONAL MATCH (association)-[association_link:HAS_PROJECTION_SUPPORT]->(support)
    OPTIONAL MATCH (input)-[input_link:HAS_PROJECTION_ASSOCIATION]->(association)
    OPTIONAL MATCH (input)-[snapshot_link:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
    OPTIONAL MATCH (support)-[observation_link:SUPPORTED_BY_MEMBERSHIP]->(observation)
    OPTIONAL MATCH (snapshot)-[snapshot_observation_link:HAS_MEMBERSHIP_OBSERVATION]->(observation)
    OPTIONAL MATCH (support)-[target_link:SUPPORTED_BY_MAPPING_TARGET]->(target)
    OPTIONAL MATCH (revision)-[entry_link:HAS_MAPPING_ENTRY]->(entry:CrmTenantMappingEntry)
      -[target_owner_link:HAS_MAPPING_TARGET]->(target)
    OPTIONAL MATCH (target)-[entity_link:TARGETS_ENTITY]->(entity:Entity)
    WITH release, support, count(DISTINCT association_link) AS associations,
      count(DISTINCT association) AS association_nodes, count(DISTINCT input_link) AS input_links,
      count(DISTINCT input) AS input_nodes, count(DISTINCT snapshot_link) AS snapshot_links,
      count(DISTINCT snapshot) AS snapshots, count(DISTINCT observation_link) AS observations,
      count(DISTINCT snapshot_observation_link) AS snapshot_observations,
      count(DISTINCT target_link) AS targets, count(DISTINCT entry_link) AS entries,
      count(DISTINCT target_owner_link) AS target_owners, count(DISTINCT entity_link) AS entities,
      collect(DISTINCT association.release_id) AS association_release_ids,
      collect(DISTINCT association.association_id) AS association_ids,
      collect(DISTINCT association.input_id) AS association_input_ids,
      collect(DISTINCT association.entity_key) AS association_entity_keys,
      collect(DISTINCT association.relationship_kind) AS association_relationship_kinds,
      collect(DISTINCT input.release_id) AS input_release_ids,
      collect(DISTINCT input.input_id) AS input_ids,
      collect(DISTINCT input.subject_kind) AS input_subject_kinds,
      collect(DISTINCT input.subject_id) AS input_subject_ids,
      collect(DISTINCT input.snapshot_id) AS input_snapshot_ids,
      collect(DISTINCT snapshot.snapshot_id) AS snapshot_ids,
      collect(DISTINCT observation.observation_id) AS observation_ids,
      collect(DISTINCT observation.snapshot_id) AS observation_snapshot_ids,
      collect(DISTINCT observation.subject_kind) AS observation_subject_kinds,
      collect(DISTINCT observation.subject_id) AS observation_subject_ids,
      collect(DISTINCT entry.revision_id) AS entry_revision_ids,
      collect(DISTINCT entry.company_id) AS entry_company_ids,
      collect(DISTINCT observation.company_id) AS observation_company_ids,
      collect(DISTINCT target.target_id) AS target_ids,
      collect(DISTINCT target.entity_key) AS target_entity_keys,
      collect(DISTINCT target.relationship_kind) AS target_relationship_kinds,
      collect(DISTINCT entity.entity_key) AS entity_keys
    WHERE associations <> 1 OR association_nodes <> 1 OR input_links <> 1 OR input_nodes <> 1
      OR snapshot_links <> 1 OR snapshots <> 1 OR observations <> 1 OR snapshot_observations <> 1
      OR targets <> 1 OR entries <> 1 OR target_owners <> 1 OR entities <> 1
      OR association_release_ids <> [release.release_id]
      OR association_ids <> [support.association_id] OR association_input_ids <> input_ids
      OR input_release_ids <> [release.release_id]
      OR snapshot_ids <> input_snapshot_ids
      OR observation_ids <> [support.membership_observation_id]
      OR observation_snapshot_ids <> snapshot_ids
      OR observation_subject_kinds <> input_subject_kinds
      OR observation_subject_ids <> input_subject_ids
      OR entry_revision_ids <> [release.mapping_revision_id]
      OR entry_company_ids <> observation_company_ids
      OR target_ids <> [support.mapping_target_id]
      OR target_entity_keys <> entity_keys OR entity_keys <> association_entity_keys
      OR target_relationship_kinds <> association_relationship_kinds
      OR support.support_id IS NULL OR support.support_digest IS NULL
      OR size(support.support_id) <> 71 OR NOT (support.support_id STARTS WITH 'sha256:')
      OR size(support.support_digest) <> 71 OR NOT (support.support_digest STARTS WITH 'sha256:')
    RETURN support
  }
SET release.state = 'completed', release.completed_at = datetime(), release.updated_at = datetime()
RETURN properties(release) AS release
"""

CANCEL_RELEASE = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint, state: 'building'
})
SET release.state = 'cancelled', release.cancelled_at = datetime(), release.updated_at = datetime()
RETURN properties(release) AS release
"""

FAIL_RELEASE = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint, state: 'building'
})
SET release.state = 'failed', release.failure_code = $failure_code, release.failed_at = datetime(),
  release.updated_at = datetime()
RETURN properties(release) AS release
"""

READ_COMPLETED = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, state: 'completed', phase: 'complete',
  capture_complete: true, projection_complete: true
})
RETURN properties(release) AS release
"""
