"""Strict exact active/completed projection authority reads for #307."""

from __future__ import annotations

VALIDATE_SOURCE_SYNC_PROJECTION = """
MATCH (head:CrmTenantProjectionActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $projection_head_id,
  active_release_fingerprint: $projection_head_digest
})
MATCH (release:CrmTenantProjectionRelease {
  release_id: head.active_release_id, state: 'published',
  release_fingerprint: head.active_release_fingerprint
})
WHERE release.source_key = $source_key
  AND release.source_instance_id = $source_instance_id
  AND release.control_instance_id = $control_instance_id
  AND release.release_number = head.active_release_number
  AND ($projection_active_release_id IS NULL
    OR (head.active_release_id = $projection_active_release_id
      AND head.active_release_number = $projection_active_release_number))
RETURN head.active_release_id AS release_id
"""

VALIDATE_MAPPING_ACTIVATION_PROJECTION = """
MATCH (release:CrmTenantProjectionRelease {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, release_id: $completed_release_id,
  release_fingerprint: $completed_release_fingerprint, state: 'completed',
  mapping_revision_id: $candidate_revision_id, mapping_manifest_digest: $candidate_manifest_digest
})
OPTIONAL MATCH (head:CrmTenantProjectionActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $expected_projection_head_id
})
WITH release, head
WHERE (($expected_projection_active_release_id IS NULL AND head IS NULL)
  OR ($expected_projection_active_release_id IS NOT NULL
    AND head.active_release_id = $expected_projection_active_release_id
    AND head.active_release_number = $expected_projection_active_release_number
    AND head.active_release_fingerprint = $expected_projection_active_release_fingerprint))
RETURN release.release_id AS release_id
"""

READ_EXACT_ACTIVE_PROJECTION_HEAD = """
MATCH (head:CrmTenantProjectionActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (release:CrmTenantProjectionRelease {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, release_id: head.active_release_id,
  release_number: head.active_release_number, release_fingerprint: head.active_release_fingerprint,
  state: 'published'
})
RETURN head.head_id AS head_id, head.active_release_id AS release_id,
  head.active_release_number AS release_number, head.active_release_fingerprint AS fingerprint
"""
