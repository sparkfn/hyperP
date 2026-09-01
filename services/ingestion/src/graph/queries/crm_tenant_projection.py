"""Parameterized guarded Cypher for immutable CRM tenant projection materialization."""

from __future__ import annotations

READ_CENSUS = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id})
MATCH (unit:StandaloneCrmCensusUnit {census_id: $census_id})
CALL {
  WITH census, unit
  OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
    census_id: census.census_id, stream_kind: unit.stream_kind
  })
  RETURN collect(properties(checkpoint)) AS checkpoints
}
WITH census, collect({
  stream_kind: unit.stream_kind, state: unit.state, generation: unit.generation,
  frozen_upper_id: unit.frozen_upper_id, checkpoints: checkpoints
}) AS units
CALL {
  WITH census
  OPTIONAL MATCH (publication:StandaloneCrmChildPublication {census_id: census.census_id})
  RETURN collect(properties(publication)) AS publications
}
CALL {
  WITH census
  OPTIONAL MATCH (fence:StandaloneCrmCensusFence {census_id: census.census_id})
  RETURN collect(properties(fence)) AS fences
}
CALL {
  WITH census
  OPTIONAL MATCH (scope:StandaloneCrmCensusActiveScope)-[:HAS_ACTIVE_CENSUS]->(census)
  RETURN count(scope) AS active_scope_count
}
RETURN properties(census) AS census, units, publications, fences, active_scope_count
"""

LOCK_SCOPE = """
MERGE (counter:CrmTenantProjectionScopeCounter {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
ON CREATE SET counter.next_release_number = 1, counter.serialization_version = 0
SET counter.serialization_version = counter.serialization_version + 1
RETURN counter.next_release_number AS next_release_number
"""

FIND_BY_REQUEST = """
MATCH (release:CrmTenantProjectionRelease {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, request_id: $request_id
})
RETURN properties(release) AS release
ORDER BY release.release_number
"""

CHECK_RELEASE_ID = """
OPTIONAL MATCH (release:CrmTenantProjectionRelease {release_id: $release_id})
RETURN count(release) AS release_count
"""


READ_MAPPING_BOUNDARY = """
MATCH (revision:CrmTenantMappingRevision {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, revision_id: $mapping_revision_id,
  manifest_digest: $mapping_manifest_digest, state: 'prepared',
  expected_head_id: $expected_mapping_head_id,
  expected_head_present: $expected_mapping_head_present
})
OPTIONAL MATCH (head:CrmTenantMappingActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $expected_mapping_head_id
})
WHERE (($expected_mapping_head_present = false
    AND revision.expected_active_revision_id IS NULL
    AND revision.expected_active_revision_number IS NULL
    AND revision.expected_active_manifest_digest IS NULL
    AND head IS NULL)
  OR ($expected_mapping_head_present = true
    AND revision.expected_active_revision_id = $expected_mapping_active_revision_id
    AND revision.expected_active_revision_number = $expected_mapping_active_revision_number
    AND revision.expected_active_manifest_digest = $expected_mapping_head_digest
    AND head.active_revision_id = $expected_mapping_active_revision_id
    AND head.active_revision_number = $expected_mapping_active_revision_number
    AND head.active_manifest_digest = $expected_mapping_head_digest))
RETURN revision.revision_number AS revision_number
"""

CREATE_RELEASE = """
MATCH (counter:CrmTenantProjectionScopeCounter {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (census:StandaloneCrmCensus {
  census_id: $source_census_id, source_key: $source_key,
  source_instance_id: $source_instance_id, control_instance_id: $control_instance_id,
  census_kind: 'source_sync', status: 'completed', fingerprint: $source_census_fingerprint
})
MATCH (revision:CrmTenantMappingRevision {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, revision_id: $mapping_revision_id,
  manifest_digest: $mapping_manifest_digest, state: 'prepared',
  expected_head_id: $expected_mapping_head_id,
  expected_head_present: $expected_mapping_head_present
})
MATCH (contact:StandaloneCrmCensusUnit {census_id: census.census_id, stream_kind: 'contact'})
MATCH (lead:StandaloneCrmCensusUnit {census_id: census.census_id, stream_kind: 'lead'})
OPTIONAL MATCH (contact_checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: census.census_id, stream_kind: 'contact'
})
OPTIONAL MATCH (lead_checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: census.census_id, stream_kind: 'lead'
})
OPTIONAL MATCH (mapping_head:CrmTenantMappingActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $expected_mapping_head_id
})
OPTIONAL MATCH (projection_head:CrmTenantProjectionActiveHead {
  source_key: $source_key, source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id, head_id: $projection_head_id
})
WITH counter, census, revision, contact, lead, contact_checkpoint, lead_checkpoint, mapping_head, projection_head
WHERE counter.next_release_number = $release_number
  AND contact.generation = $properties.contact_unit_generation
  AND lead.generation = $properties.lead_unit_generation
  AND contact.state = $properties.contact_unit_state AND lead.state = $properties.lead_unit_state
  AND contact.frozen_upper_id = $properties.contact_frozen_upper_id
  AND lead.frozen_upper_id = $properties.lead_frozen_upper_id
  AND ((contact.state = 'completed'
    AND $properties.contact_checkpoint_present = true
    AND contact_checkpoint IS NOT NULL
    AND NOT EXISTS {
      MATCH (other_contact_checkpoint:StandaloneCrmCensusCheckpoint {
        census_id: census.census_id, stream_kind: 'contact'
      }) WHERE id(other_contact_checkpoint) <> id(contact_checkpoint)
    }
    AND contact_checkpoint.generation = $properties.contact_checkpoint_generation
    AND contact_checkpoint.frozen_upper_id = $properties.contact_frozen_upper_id
    AND contact_checkpoint.last_committed_id = $properties.contact_frozen_upper_id
    AND contact_checkpoint.processed_rows = $properties.contact_processed_rows
    AND contact_checkpoint.skipped_rows = $properties.contact_skipped_rows)
    OR (contact.state = 'no_work'
      AND $properties.contact_checkpoint_present = false
      AND $properties.contact_unit_generation > 0
      AND $properties.contact_frozen_upper_id = 0
      AND $properties.contact_processed_rows = 0
      AND $properties.contact_skipped_rows = 0
      AND NOT EXISTS {
        MATCH (:StandaloneCrmCensusCheckpoint {
          census_id: census.census_id, stream_kind: 'contact'
        })
      }))
  AND ((lead.state = 'completed'
    AND $properties.lead_checkpoint_present = true
    AND lead_checkpoint IS NOT NULL
    AND NOT EXISTS {
      MATCH (other_lead_checkpoint:StandaloneCrmCensusCheckpoint {
        census_id: census.census_id, stream_kind: 'lead'
      }) WHERE id(other_lead_checkpoint) <> id(lead_checkpoint)
    }
    AND lead_checkpoint.generation = $properties.lead_checkpoint_generation
    AND lead_checkpoint.frozen_upper_id = $properties.lead_frozen_upper_id
    AND lead_checkpoint.last_committed_id = $properties.lead_frozen_upper_id
    AND lead_checkpoint.processed_rows = $properties.lead_processed_rows
    AND lead_checkpoint.skipped_rows = $properties.lead_skipped_rows)
    OR (lead.state = 'no_work'
      AND $properties.lead_checkpoint_present = false
      AND $properties.lead_unit_generation > 0
      AND $properties.lead_frozen_upper_id = 0
      AND $properties.lead_processed_rows = 0
      AND $properties.lead_skipped_rows = 0
      AND NOT EXISTS {
        MATCH (:StandaloneCrmCensusCheckpoint {
          census_id: census.census_id, stream_kind: 'lead'
        })
      }))
  AND (($expected_mapping_head_present = false
      AND revision.expected_active_revision_id IS NULL
      AND revision.expected_active_revision_number IS NULL
      AND revision.expected_active_manifest_digest IS NULL
      AND mapping_head IS NULL)
    OR ($expected_mapping_head_present = true
      AND revision.expected_active_revision_id = $expected_mapping_active_revision_id
      AND revision.expected_active_revision_number = $expected_mapping_active_revision_number
      AND revision.expected_active_manifest_digest = $expected_mapping_head_digest
      AND mapping_head.active_revision_id = $expected_mapping_active_revision_id
      AND mapping_head.active_revision_number = $expected_mapping_active_revision_number
      AND mapping_head.active_manifest_digest = $expected_mapping_head_digest))
  AND (($expected_prior_head_present = false AND projection_head IS NULL)
    OR ($expected_prior_head_present = true
      AND $expected_prior_head_id = $projection_head_id
      AND projection_head.active_release_id = $expected_prior_release_id
      AND projection_head.active_release_number = $expected_prior_release_number
      AND projection_head.active_release_fingerprint = $expected_prior_release_fingerprint
      AND $release_number > $expected_prior_release_number))
CREATE (release:CrmTenantProjectionRelease $properties)
SET release.created_at = datetime(),
  counter.next_release_number = counter.next_release_number + 1
CREATE (release)-[:MATERIALIZES_MAPPING_REVISION]->(revision)
CREATE (release)-[:MATERIALIZES_SOURCE_CENSUS]->(census)
RETURN properties(release) AS release
"""

READ_RELEASE = """
MATCH (release:CrmTenantProjectionRelease {release_id: $release_id})
RETURN properties(release) AS release
"""


READ_CAPTURE_COUNTS = """
MATCH (release:CrmTenantProjectionRelease {release_id: $release_id})
RETURN release.contact_input_count AS contact_input_count,
  release.lead_input_count AS lead_input_count
"""

CAPTURE_CANDIDATES = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, state: 'building', phase: 'capture'
})
MATCH (census:StandaloneCrmCensus {
  census_id: release.source_census_id, source_key: release.source_key,
  source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, census_kind: 'source_sync',
  status: 'completed', fingerprint: release.source_census_fingerprint
})
MATCH (head:CrmCompanyMembershipHead {
  source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id
})-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot:CrmCompanyMembershipSnapshot)
MATCH (unit:StandaloneCrmCensusUnit {
  census_id: census.census_id, stream_kind: head.subject_kind
})
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: census.census_id, stream_kind: head.subject_kind
})
WITH release, census, head, snapshot, unit, checkpoint
WHERE head.subject_kind IN ['contact', 'lead']
  AND ((head.subject_kind = 'contact'
      AND unit.state = release.contact_unit_state
      AND unit.generation = release.contact_unit_generation
      AND unit.frozen_upper_id = release.contact_frozen_upper_id)
    OR (head.subject_kind = 'lead'
      AND unit.state = release.lead_unit_state
      AND unit.generation = release.lead_unit_generation
      AND unit.frozen_upper_id = release.lead_frozen_upper_id))
  AND ((head.subject_kind = 'contact'
      AND ((unit.state = 'completed' AND checkpoint.generation = release.contact_checkpoint_generation
          AND NOT EXISTS {
            MATCH (other_contact_checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: census.census_id, stream_kind: 'contact'
            }) WHERE id(other_contact_checkpoint) <> id(checkpoint)
          }
          AND checkpoint.frozen_upper_id = release.contact_frozen_upper_id
          AND checkpoint.last_committed_id = release.contact_frozen_upper_id
          AND checkpoint.processed_rows = release.contact_processed_rows
          AND checkpoint.skipped_rows = release.contact_skipped_rows)
        OR (unit.state = 'no_work' AND release.contact_checkpoint_present = false
          AND release.contact_unit_generation > 0 AND release.contact_frozen_upper_id = 0
          AND release.contact_processed_rows = 0 AND release.contact_skipped_rows = 0
          AND checkpoint IS NULL)))
    OR (head.subject_kind = 'lead'
      AND ((unit.state = 'completed' AND checkpoint.generation = release.lead_checkpoint_generation
          AND NOT EXISTS {
            MATCH (other_lead_checkpoint:StandaloneCrmCensusCheckpoint {
              census_id: census.census_id, stream_kind: 'lead'
            }) WHERE id(other_lead_checkpoint) <> id(checkpoint)
          }
          AND checkpoint.frozen_upper_id = release.lead_frozen_upper_id
          AND checkpoint.last_committed_id = release.lead_frozen_upper_id
          AND checkpoint.processed_rows = release.lead_processed_rows
          AND checkpoint.skipped_rows = release.lead_skipped_rows)
        OR (unit.state = 'no_work' AND release.lead_checkpoint_present = false
          AND release.lead_unit_generation > 0 AND release.lead_frozen_upper_id = 0
          AND release.lead_processed_rows = 0 AND release.lead_skipped_rows = 0
          AND checkpoint IS NULL))))
  AND head.selected_snapshot_id = snapshot.snapshot_id
  AND snapshot.source_instance_id = release.source_instance_id
  AND snapshot.control_instance_id = release.control_instance_id
  AND snapshot.subject_kind = head.subject_kind
  AND snapshot.subject_id = head.subject_id
  AND head.available_at = census.created_at AND snapshot.available_at = census.created_at
  AND head.source_record_version = snapshot.source_record_version
  AND head.source_record_pk = snapshot.source_record_pk
  AND toInteger(head.subject_id) <= unit.frozen_upper_id
  AND toInteger(snapshot.subject_id) <= unit.frozen_upper_id
  AND toString(toInteger(head.subject_id)) = head.subject_id
  AND toString(toInteger(snapshot.subject_id)) = snapshot.subject_id
  AND size([(head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->() | 1]) = 1
  AND (release.capture_cursor_kind IS NULL
    OR CASE head.subject_kind WHEN 'contact' THEN 0 ELSE 1 END
       > CASE release.capture_cursor_kind WHEN 'contact' THEN 0 ELSE 1 END
    OR (head.subject_kind = release.capture_cursor_kind
      AND toInteger(head.subject_id) > release.capture_cursor_subject_id))
RETURN head.subject_kind AS subject_kind, head.subject_id AS subject_id,
  snapshot.snapshot_id AS snapshot_id, snapshot.snapshot_digest AS snapshot_digest
ORDER BY CASE head.subject_kind WHEN 'contact' THEN 0 ELSE 1 END, toInteger(head.subject_id)
LIMIT $page_limit
"""

WRITE_INPUTS = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'capture'
})-[:MATERIALIZES_SOURCE_CENSUS]->(census:StandaloneCrmCensus {
  census_id: release.source_census_id, fingerprint: release.source_census_fingerprint,
  status: 'completed'
})
MATCH (release)-[:MATERIALIZES_MAPPING_REVISION]->(revision:CrmTenantMappingRevision {
  revision_id: release.mapping_revision_id,
  manifest_digest: release.mapping_manifest_digest, state: 'prepared'
})
UNWIND $inputs AS item
MATCH (head:CrmCompanyMembershipHead {
  source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, subject_kind: item.subject_kind,
  subject_id: item.subject_id
})-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot:CrmCompanyMembershipSnapshot {
  snapshot_id: item.snapshot_id, snapshot_digest: item.snapshot_digest,
  source_instance_id: release.source_instance_id,
  control_instance_id: release.control_instance_id, subject_kind: item.subject_kind,
  subject_id: item.subject_id
})
WHERE head.selected_snapshot_id = snapshot.snapshot_id
  AND head.available_at = census.created_at AND snapshot.available_at = census.created_at
  AND head.source_record_version = snapshot.source_record_version
  AND head.source_record_pk = snapshot.source_record_pk
  AND size([(head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->() | 1]) = 1
MERGE (input:CrmTenantProjectionInput {
  release_id: $release_id, subject_kind: item.subject_kind, subject_id: item.subject_id
})
ON CREATE SET input.input_id = item.input_id, input.input_digest = item.input_digest,
  input.snapshot_id = item.snapshot_id, input.snapshot_digest = item.snapshot_digest,
  input.created_at = datetime()
WITH release, input, snapshot, item
WHERE input.input_id = item.input_id AND input.input_digest = item.input_digest
  AND input.snapshot_id = item.snapshot_id AND input.snapshot_digest = item.snapshot_digest
MERGE (release)-[:HAS_PROJECTION_INPUT]->(input)
MERGE (input)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
RETURN count(input) AS input_count
"""

ADVANCE_CAPTURE = """
MATCH (release:CrmTenantProjectionRelease {
  release_id: $release_id, release_fingerprint: $release_fingerprint,
  state: 'building', phase: 'capture'
})
WHERE release.input_count = $prior_input_count
  AND release.contact_input_count = $prior_contact_input_count
  AND release.lead_input_count = $prior_lead_input_count
  AND (NOT $done OR (
    $contact_input_count = release.contact_expected_input_count
    AND $lead_input_count = release.lead_expected_input_count
    AND $input_count = release.contact_expected_input_count + release.lead_expected_input_count
  ))
SET release.capture_cursor_kind = $cursor_kind,
  release.capture_cursor_subject_id = $cursor_subject_id,
  release.input_count = $input_count,
  release.contact_input_count = $contact_input_count,
  release.lead_input_count = $lead_input_count,
  release.capture_boundary_digest = $capture_boundary_digest,
  release.phase = CASE WHEN $done THEN 'projection' ELSE 'capture' END,
  release.capture_complete = $done, release.updated_at = datetime()
RETURN properties(release) AS release
"""
