"""Parameterized one-transaction Cypher for #307 mapping/projection activation."""

from __future__ import annotations

LOCK_ACTIVATION_SCOPE = """
MERGE (mapping_counter:CrmTenantMappingScopeCounter {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
ON CREATE SET mapping_counter.next_revision_number = 0, mapping_counter.serialization_version = 0
SET mapping_counter.serialization_version = coalesce(mapping_counter.serialization_version, 0) + 1
WITH mapping_counter
MERGE (projection_counter:CrmTenantProjectionScopeCounter {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
ON CREATE SET projection_counter.next_release_number = 1, projection_counter.serialization_version = 0
SET projection_counter.serialization_version = coalesce(projection_counter.serialization_version, 0) + 1
RETURN mapping_counter.serialization_version AS mapping_version,
  projection_counter.serialization_version AS projection_version
"""

READ_RECEIPT = """
MATCH (release:CrmTenantProjectionRelease {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, release_id: $release_id,
  release_fingerprint: $release_fingerprint, state: 'published',
  activation_census_id: $census_id, activation_generation: $generation,
  activation_task_id: $task_id, activation_candidate_revision_id: $candidate_revision_id,
  activation_candidate_manifest_digest: $candidate_manifest_digest,
  activation_mapping_head_present: $mapping_head_present,
  activation_projection_head_present: $projection_head_present
})
WHERE (($mapping_head_present = false
    AND release.activation_prior_mapping_head_id IS NULL
    AND release.activation_prior_mapping_revision_id IS NULL
    AND release.activation_prior_mapping_revision_number IS NULL
    AND release.activation_prior_mapping_manifest_digest IS NULL)
  OR ($mapping_head_present = true
    AND release.activation_prior_mapping_head_id = $expected_mapping_head_id
    AND release.activation_prior_mapping_revision_id = $expected_mapping_revision_id
    AND release.activation_prior_mapping_revision_number = $expected_mapping_revision_number
    AND release.activation_prior_mapping_manifest_digest = $expected_mapping_manifest_digest))
  AND (($projection_head_present = false
    AND release.activation_prior_projection_head_id IS NULL
    AND release.activation_prior_projection_release_id IS NULL
    AND release.activation_prior_projection_release_number IS NULL
    AND release.activation_prior_projection_release_fingerprint IS NULL)
  OR ($projection_head_present = true
    AND release.activation_prior_projection_head_id = $expected_projection_head_id
    AND release.activation_prior_projection_release_id = $expected_projection_release_id
    AND release.activation_prior_projection_release_number = $expected_projection_release_number
    AND release.activation_prior_projection_release_fingerprint = $expected_projection_release_fingerprint))
RETURN properties(release) AS release
"""

ACTIVATE = """
MATCH (candidate:CrmTenantMappingRevision {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, revision_id: $candidate_revision_id,
  manifest_digest: $candidate_manifest_digest, state: 'prepared'
})
MATCH (release:CrmTenantProjectionRelease {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, release_id: $release_id,
  release_fingerprint: $release_fingerprint, state: 'completed',
  mapping_revision_id: $candidate_revision_id,
  mapping_manifest_digest: $candidate_manifest_digest
})
OPTIONAL MATCH (mapping_head:CrmTenantMappingActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $mapping_head_id
})
OPTIONAL MATCH (projection_head:CrmTenantProjectionActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $projection_head_id
})
WITH candidate, release, mapping_head, projection_head, datetime() AS activated_at
WITH candidate, release, mapping_head, projection_head, toString(activated_at) AS activated_at
WHERE $expected_mapping_head_id = $mapping_head_id
  AND ($projection_head_present = false OR $expected_projection_head_id = $projection_head_id)
  AND (($mapping_head_present = false AND mapping_head IS NULL)
  OR ($mapping_head_present = true
    AND mapping_head.active_revision_id = $expected_mapping_revision_id
    AND mapping_head.active_revision_number = $expected_mapping_revision_number
    AND mapping_head.active_manifest_digest = $expected_mapping_manifest_digest
    AND candidate.revision_number > mapping_head.active_revision_number))
  AND (($projection_head_present = false AND projection_head IS NULL)
  OR ($projection_head_present = true
    AND projection_head.active_release_id = $expected_projection_release_id
    AND projection_head.active_release_number = $expected_projection_release_number
    AND projection_head.active_release_fingerprint = $expected_projection_release_fingerprint
    AND release.release_number > projection_head.active_release_number))
  AND candidate.expected_head_id = $expected_mapping_head_id
  AND candidate.expected_head_present = $mapping_head_present
  AND (($mapping_head_present = false
    AND candidate.expected_active_revision_id IS NULL
    AND candidate.expected_active_revision_number IS NULL
    AND candidate.expected_active_manifest_digest IS NULL)
    OR ($mapping_head_present = true
    AND candidate.expected_active_revision_id = $expected_mapping_revision_id
    AND candidate.expected_active_revision_number = $expected_mapping_revision_number
    AND candidate.expected_active_manifest_digest = $expected_mapping_manifest_digest))
  AND release.expected_prior_head_present = $projection_head_present
  AND (($projection_head_present = false
    AND release.expected_prior_head_id IS NULL
    AND release.expected_prior_release_id IS NULL
    AND release.expected_prior_release_number IS NULL
    AND release.expected_prior_release_fingerprint IS NULL)
    OR ($projection_head_present = true
    AND release.expected_prior_head_id = $expected_projection_head_id
    AND release.expected_prior_release_id = $expected_projection_release_id
    AND release.expected_prior_release_number = $expected_projection_release_number
    AND release.expected_prior_release_fingerprint = $expected_projection_release_fingerprint))
OPTIONAL MATCH (prior_mapping:CrmTenantMappingRevision {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, revision_id: $expected_mapping_revision_id,
  manifest_digest: $expected_mapping_manifest_digest
})
WITH candidate, release, mapping_head, projection_head, prior_mapping, activated_at
WHERE ($mapping_head_present = false AND prior_mapping IS NULL)
  OR ($mapping_head_present = true
    AND prior_mapping.state = 'active'
    AND prior_mapping.revision_number = $expected_mapping_revision_number)
FOREACH (_ IN CASE WHEN $mapping_head_present THEN [1] ELSE [] END |
  SET prior_mapping.state = 'superseded', prior_mapping.superseded_at = activated_at
)
SET candidate.state = 'active', candidate.activated_at = activated_at,
  release.state = 'published', release.published_at = activated_at,
  release.activation_census_id = $census_id, release.activation_generation = $generation,
  release.activation_task_id = $task_id,
  release.activation_candidate_revision_id = $candidate_revision_id,
  release.activation_candidate_manifest_digest = $candidate_manifest_digest,
  release.activation_mapping_head_present = $mapping_head_present,
  release.activation_prior_mapping_head_id = $expected_mapping_head_id,
  release.activation_prior_mapping_revision_id = $expected_mapping_revision_id,
  release.activation_prior_mapping_revision_number = $expected_mapping_revision_number,
  release.activation_prior_mapping_manifest_digest = $expected_mapping_manifest_digest,
  release.activation_projection_head_present = $projection_head_present,
  release.activation_prior_projection_head_id = $expected_projection_head_id,
  release.activation_prior_projection_release_id = $expected_projection_release_id,
  release.activation_prior_projection_release_number = $expected_projection_release_number,
  release.activation_prior_projection_release_fingerprint = $expected_projection_release_fingerprint,
  release.activation_activated_at = activated_at
MERGE (new_mapping_head:CrmTenantMappingActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
ON CREATE SET new_mapping_head.head_id = $mapping_head_id
WITH candidate, release, activated_at, new_mapping_head
WHERE new_mapping_head.head_id = $mapping_head_id
SET new_mapping_head.active_revision_id = candidate.revision_id,
  new_mapping_head.active_revision_number = candidate.revision_number,
  new_mapping_head.active_manifest_digest = candidate.manifest_digest,
  new_mapping_head.effective_at = activated_at
MERGE (new_projection_head:CrmTenantProjectionActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
ON CREATE SET new_projection_head.head_id = $projection_head_id
WITH release, activated_at, new_projection_head
WHERE new_projection_head.head_id = $projection_head_id
SET new_projection_head.active_release_id = release.release_id,
  new_projection_head.active_release_number = release.release_number,
  new_projection_head.active_release_fingerprint = release.release_fingerprint,
  new_projection_head.effective_at = activated_at
RETURN properties(release) AS release
"""

READ_RECEIPT_BY_ID = """
MATCH (release:CrmTenantProjectionRelease {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, release_id: $release_id,
  release_fingerprint: $release_fingerprint, state: 'published',
  activation_census_id: $census_id, activation_generation: $generation,
  activation_task_id: $task_id
})
RETURN properties(release) AS release
"""
