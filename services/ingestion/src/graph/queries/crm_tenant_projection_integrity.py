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
WITH DISTINCT release, contact, lead, contact_checkpoint, lead_checkpoint, revision, mapping_head,
  projection_head
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
