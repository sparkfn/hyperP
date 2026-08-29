"""Parameterized Neo4j queries for atomic CRM company and membership commits."""

from __future__ import annotations

READ_CENSUS_REQUEST = """
MATCH (census:StandaloneCrmCensus {census_id: $census_id, generation: $generation})
RETURN census.request_json AS request_json
"""

CLAIM_DESCRIPTION_TRANSITION = """
MATCH (census:StandaloneCrmCensus {
  census_id: $census_id,
  generation: $generation,
  source_key: $source_key,
  source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id,
  census_kind: 'source_sync',
  request_json: $request_json
})
MATCH (attempt:StandaloneCrmCensusAttempt {
  census_id: $census_id,
  generation: $generation,
  fence_token: $fence_token,
  status: 'running',
  attempt_deadline: datetime($attempt_deadline)
})
MATCH (unit:StandaloneCrmCensusUnit {
  census_id: $census_id,
  generation: $generation,
  stream_kind: 'company',
  state: 'running',
  frozen_upper_id: $frozen_upper_id
})
MATCH (fence:StandaloneCrmCensusFence {
  census_id: $census_id,
  generation: $generation,
  stream_kind: 'company',
  token: $fence_token,
  owner_id: $fence_owner_id,
  status: 'active'
})
MATCH (:StandaloneCrmChildPublication {
  census_id: $census_id,
  generation: $generation,
  stream_kind: 'company',
  task_name: $task_name,
  task_id: $task_id,
  payload_digest: $payload_digest,
  status: 'published'
})
MATCH (:BitrixSourceInstance {
  source_key: $source_key,
  source_instance_id: $source_instance_id,
  status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: $source_key, is_active: true})
MATCH (:BitrixExecutionSourceBinding {
  source_key: $source_key,
  source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
SET census.crm_company_membership_lock = true
REMOVE census.crm_company_membership_lock
WITH census, attempt, fence
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: $census_id,
  stream_kind: 'company'
})
OPTIONAL MATCH (head:CrmCompanyDescriptionHead {
  source_instance_id: $source_instance_id,
  company_id: $company_id
})
WITH census, attempt, fence, checkpoint, head
WHERE census.status IN ['running', 'publishing', 'recovering']
  AND coalesce(census.cancel_requested, false) = false
  AND census.created_at = datetime($available_at)
  AND $proposed_available_at = $available_at
  AND toInteger($company_id) <= $frozen_upper_id
  AND $proposed_cursor = toInteger($company_id)
  AND fence.lease_until >= datetime()
  AND datetime() < attempt.attempt_deadline
  AND datetime() < datetime($occurrence_deadline)
WITH census, attempt, checkpoint, head,
  CASE
    WHEN checkpoint IS NULL THEN $expected_checkpoint_absent
    ELSE checkpoint.last_committed_id = $expected_cursor
      AND checkpoint.processed_rows = $expected_processed
      AND checkpoint.skipped_rows = $expected_skipped
      AND checkpoint.generation = $generation
      AND checkpoint.fence_token = $fence_token
      AND checkpoint.frozen_upper_id = $frozen_upper_id
      AND checkpoint.revision_id IS NULL
      AND checkpoint.binding_subject_id IS NULL
      AND checkpoint.binding_offset IS NULL
  END AS checkpoint_expected,
  CASE
    WHEN head IS NULL THEN $expected_head_id IS NULL
    ELSE $expected_head_id IS NOT NULL
      AND $expected_head_at IS NOT NULL
      AND $expected_head_version IS NOT NULL
      AND $expected_head_pk IS NOT NULL
      AND head.control_instance_id = $control_instance_id
      AND head.selected_observation_id = $expected_head_id
      AND head.available_at = datetime($expected_head_at)
      AND head.source_record_version = $expected_head_version
      AND head.source_record_pk = $expected_head_pk
  END AS head_expected,
  CASE
    WHEN checkpoint IS NULL THEN false
    ELSE checkpoint.last_committed_id = $proposed_cursor
      AND checkpoint.processed_rows = $proposed_processed
      AND checkpoint.skipped_rows = $proposed_skipped
      AND checkpoint.generation = $generation
      AND checkpoint.fence_token = $fence_token
      AND checkpoint.frozen_upper_id = $frozen_upper_id
      AND checkpoint.revision_id IS NULL
      AND checkpoint.binding_subject_id IS NULL
      AND checkpoint.binding_offset IS NULL
  END AS checkpoint_is_proposed,
  CASE
    WHEN head IS NULL THEN false
    ELSE head.control_instance_id = $control_instance_id
      AND head.selected_observation_id = $proposed_head_id
      AND head.available_at = datetime($proposed_available_at)
      AND head.source_record_version = $proposed_head_version
      AND head.source_record_pk = $proposed_head_pk
  END AS head_is_proposed
WITH census, attempt, checkpoint, head, checkpoint_expected, head_expected,
  checkpoint_is_proposed, head_is_proposed,
  CASE
    WHEN head IS NULL THEN true
    WHEN head.available_at < datetime($proposed_available_at) THEN true
    WHEN head.available_at > datetime($proposed_available_at) THEN false
    WHEN head.source_record_version < $proposed_head_version THEN true
    WHEN head.source_record_version > $proposed_head_version THEN false
    ELSE head.source_record_pk < $proposed_head_pk
  END AS proposed_is_forward
WITH census, attempt, checkpoint, head,
  CASE
    WHEN checkpoint_is_proposed AND head_is_proposed THEN 'idempotent'
    WHEN NOT checkpoint_expected OR NOT head_expected OR NOT proposed_is_forward
      THEN 'stale_or_conflict'
    WHEN coalesce(census.occurrence_rows, 0) + $processed_delta > $occurrence_row_limit
      THEN 'occurrence_exhausted'
    WHEN coalesce(attempt.row_count, 0) + $processed_delta > $attempt_row_limit
      THEN 'attempt_exhausted'
    ELSE 'committed'
  END AS decision
FOREACH (_ IN CASE WHEN decision = 'committed' THEN [1] ELSE [] END |
  MERGE (stored:StandaloneCrmCensusCheckpoint {
    census_id: $census_id,
    stream_kind: 'company'
  })
  SET stored.last_committed_id = $proposed_cursor,
      stored.processed_rows = $proposed_processed,
      stored.skipped_rows = $proposed_skipped,
      stored.binding_subject_id = null,
      stored.binding_offset = null,
      stored.generation = $generation,
      stored.fence_token = $fence_token,
      stored.frozen_upper_id = $frozen_upper_id,
      stored.revision_id = null,
      stored.updated_at = datetime(),
      census.occurrence_rows = coalesce(census.occurrence_rows, 0) + $processed_delta,
      attempt.row_count = coalesce(attempt.row_count, 0) + $processed_delta
  MERGE (selected:CrmCompanyDescriptionHead {
    source_instance_id: $source_instance_id,
    company_id: $company_id
  })
  SET selected.control_instance_id = $control_instance_id,
      selected.selected_observation_id = $proposed_head_id,
      selected.available_at = datetime($proposed_available_at),
      selected.source_record_version = $proposed_head_version,
      selected.source_record_pk = $proposed_head_pk,
      selected.updated_at = datetime()
)
RETURN decision AS decision
"""

CLAIM_MEMBERSHIP_TRANSITION = """
MATCH (census:StandaloneCrmCensus {
  census_id: $census_id,
  generation: $generation,
  source_key: $source_key,
  source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id,
  census_kind: 'source_sync',
  request_json: $request_json
})
MATCH (attempt:StandaloneCrmCensusAttempt {
  census_id: $census_id,
  generation: $generation,
  fence_token: $fence_token,
  status: 'running',
  attempt_deadline: datetime($attempt_deadline)
})
MATCH (unit:StandaloneCrmCensusUnit {
  census_id: $census_id,
  generation: $generation,
  stream_kind: $stream_kind,
  state: 'running',
  frozen_upper_id: $frozen_upper_id
})
MATCH (fence:StandaloneCrmCensusFence {
  census_id: $census_id,
  generation: $generation,
  stream_kind: $stream_kind,
  token: $fence_token,
  owner_id: $fence_owner_id,
  status: 'active'
})
MATCH (:StandaloneCrmChildPublication {
  census_id: $census_id,
  generation: $generation,
  stream_kind: $stream_kind,
  task_name: $task_name,
  task_id: $task_id,
  payload_digest: $payload_digest,
  status: 'published'
})
MATCH (:BitrixSourceInstance {
  source_key: $source_key,
  source_instance_id: $source_instance_id,
  status: 'active'
})-[:INSTANCE_OF]->(:SourceSystem {source_key: $source_key, is_active: true})
MATCH (:BitrixExecutionSourceBinding {
  source_key: $source_key,
  source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
SET census.crm_company_membership_lock = true
REMOVE census.crm_company_membership_lock
WITH census, attempt, fence
OPTIONAL MATCH (checkpoint:StandaloneCrmCensusCheckpoint {
  census_id: $census_id,
  stream_kind: $stream_kind
})
OPTIONAL MATCH (head:CrmCompanyMembershipHead {
  source_instance_id: $source_instance_id,
  subject_kind: $subject_kind,
  subject_id: $subject_id
})
WITH census, attempt, fence, checkpoint, head
WHERE census.status IN ['running', 'publishing', 'recovering']
  AND coalesce(census.cancel_requested, false) = false
  AND census.created_at = datetime($available_at)
  AND $proposed_available_at = $available_at
  AND toInteger($subject_id) <= $frozen_upper_id
  AND (($stream_kind = 'contact'
      AND $proposed_cursor = $expected_cursor
      AND $expected_binding_subject = toInteger($subject_id)
      AND $proposed_binding_subject = toInteger($subject_id))
    OR ($stream_kind = 'lead'
      AND $proposed_cursor = toInteger($subject_id)
      AND $expected_binding_subject IS NULL
      AND $expected_binding_offset IS NULL
      AND $proposed_binding_subject IS NULL
      AND $proposed_binding_offset IS NULL))
  AND fence.lease_until >= datetime()
  AND datetime() < attempt.attempt_deadline
  AND datetime() < datetime($occurrence_deadline)
WITH census, attempt, checkpoint, head,
  CASE
    WHEN checkpoint IS NULL THEN $expected_checkpoint_absent
    ELSE checkpoint.last_committed_id = $expected_cursor
      AND checkpoint.processed_rows = $expected_processed
      AND checkpoint.skipped_rows = $expected_skipped
      AND checkpoint.generation = $generation
      AND checkpoint.fence_token = $fence_token
      AND checkpoint.frozen_upper_id = $frozen_upper_id
      AND checkpoint.revision_id IS NULL
      AND ((checkpoint.binding_subject_id IS NULL AND $expected_binding_subject IS NULL)
        OR checkpoint.binding_subject_id = $expected_binding_subject)
      AND ((checkpoint.binding_offset IS NULL AND $expected_binding_offset IS NULL)
        OR checkpoint.binding_offset = $expected_binding_offset)
  END AS checkpoint_expected,
  CASE
    WHEN head IS NULL THEN $expected_head_id IS NULL
    ELSE $expected_head_id IS NOT NULL
      AND $expected_head_at IS NOT NULL
      AND $expected_head_version IS NOT NULL
      AND $expected_head_pk IS NOT NULL
      AND head.control_instance_id = $control_instance_id
      AND head.selected_snapshot_id = $expected_head_id
      AND head.available_at = datetime($expected_head_at)
      AND head.source_record_version = $expected_head_version
      AND head.source_record_pk = $expected_head_pk
  END AS head_expected,
  CASE
    WHEN checkpoint IS NULL THEN false
    ELSE checkpoint.last_committed_id = $proposed_cursor
      AND checkpoint.processed_rows = $proposed_processed
      AND checkpoint.skipped_rows = $proposed_skipped
      AND checkpoint.generation = $generation
      AND checkpoint.fence_token = $fence_token
      AND checkpoint.frozen_upper_id = $frozen_upper_id
      AND checkpoint.revision_id IS NULL
      AND ((checkpoint.binding_subject_id IS NULL AND $proposed_binding_subject IS NULL)
        OR checkpoint.binding_subject_id = $proposed_binding_subject)
      AND ((checkpoint.binding_offset IS NULL AND $proposed_binding_offset IS NULL)
        OR checkpoint.binding_offset = $proposed_binding_offset)
  END AS checkpoint_is_proposed,
  CASE
    WHEN head IS NULL THEN false
    ELSE head.control_instance_id = $control_instance_id
      AND head.selected_snapshot_id = $proposed_head_id
      AND head.available_at = datetime($proposed_available_at)
      AND head.source_record_version = $proposed_head_version
      AND head.source_record_pk = $proposed_head_pk
  END AS head_is_proposed
WITH census, attempt, checkpoint, head, checkpoint_expected, head_expected,
  checkpoint_is_proposed, head_is_proposed,
  CASE
    WHEN head IS NULL THEN true
    WHEN head.available_at < datetime($proposed_available_at) THEN true
    WHEN head.available_at > datetime($proposed_available_at) THEN false
    WHEN head.source_record_version < $proposed_head_version THEN true
    WHEN head.source_record_version > $proposed_head_version THEN false
    ELSE head.source_record_pk < $proposed_head_pk
  END AS proposed_is_forward
WITH census, attempt, checkpoint, head,
  CASE
    WHEN checkpoint_is_proposed AND head_is_proposed THEN 'idempotent'
    WHEN NOT checkpoint_expected OR NOT head_expected OR NOT proposed_is_forward
      THEN 'stale_or_conflict'
    WHEN coalesce(census.occurrence_rows, 0) + $processed_delta > $occurrence_row_limit
      THEN 'occurrence_exhausted'
    WHEN coalesce(attempt.row_count, 0) + $processed_delta > $attempt_row_limit
      THEN 'attempt_exhausted'
    ELSE 'committed'
  END AS decision
FOREACH (_ IN CASE WHEN decision = 'committed' THEN [1] ELSE [] END |
  MERGE (stored:StandaloneCrmCensusCheckpoint {
    census_id: $census_id,
    stream_kind: $stream_kind
  })
  SET stored.last_committed_id = $proposed_cursor,
      stored.processed_rows = $proposed_processed,
      stored.skipped_rows = $proposed_skipped,
      stored.binding_subject_id = $proposed_binding_subject,
      stored.binding_offset = $proposed_binding_offset,
      stored.generation = $generation,
      stored.fence_token = $fence_token,
      stored.frozen_upper_id = $frozen_upper_id,
      stored.revision_id = null,
      stored.updated_at = datetime(),
      census.occurrence_rows = coalesce(census.occurrence_rows, 0) + $processed_delta,
      attempt.row_count = coalesce(attempt.row_count, 0) + $processed_delta
  MERGE (selected:CrmCompanyMembershipHead {
    source_instance_id: $source_instance_id,
    subject_kind: $subject_kind,
    subject_id: $subject_id
  })
  SET selected.control_instance_id = $control_instance_id,
      selected.selected_snapshot_id = $proposed_head_id,
      selected.available_at = datetime($proposed_available_at),
      selected.source_record_version = $proposed_head_version,
      selected.source_record_pk = $proposed_head_pk,
      selected.updated_at = datetime()
)
RETURN decision AS decision
"""

UPSERT_COMPANY_REFERENCE = """
MERGE (reference:CrmCompanyReference {
  source_instance_id: $source_instance_id,
  company_id: $company_id
})
ON CREATE SET reference.source_key = $source_key,
  reference.control_instance_id = $control_instance_id,
  reference.source_record_id = $source_record_id,
  reference.identity_policy_version = $identity_policy_version,
  reference.person_matching_prohibited = true,
  reference.created_at = datetime()
WITH reference
WHERE reference.source_key = $source_key
  AND reference.control_instance_id = $control_instance_id
  AND reference.source_record_id = $source_record_id
  AND reference.identity_policy_version = $identity_policy_version
  AND reference.person_matching_prohibited = true
RETURN reference.company_id AS company_id
"""

UPSERT_DESCRIPTION_OBSERVATION = """
MATCH (reference:CrmCompanyReference {
  source_instance_id: $source_instance_id,
  company_id: $company_id
})
MATCH (head:CrmCompanyDescriptionHead {
  source_instance_id: $source_instance_id,
  company_id: $company_id,
  control_instance_id: $control_instance_id,
  selected_observation_id: $observation_id
})
MERGE (observation:CrmCompanyDescriptionObservation {observation_id: $observation_id})
ON CREATE SET observation.observation_digest = $observation_digest,
  observation.source_instance_id = $source_instance_id,
  observation.control_instance_id = $control_instance_id,
  observation.company_id = $company_id,
  observation.source_record_id = $source_record_id,
  observation.source_record_pk = $source_record_pk,
  observation.source_record_version = $source_record_version,
  observation.source_record_hash = $source_record_hash,
  observation.description = $description,
  observation.observed_at = CASE
    WHEN $observed_at IS NULL THEN null ELSE datetime($observed_at) END,
  observation.available_at = datetime($available_at),
  observation.contract_version = $contract_version,
  observation.created_at = datetime()
WITH reference, head, observation
WHERE observation.observation_digest = $observation_digest
  AND observation.source_instance_id = $source_instance_id
  AND observation.control_instance_id = $control_instance_id
  AND observation.company_id = $company_id
  AND observation.source_record_id = $source_record_id
  AND observation.source_record_pk = $source_record_pk
  AND observation.source_record_version = $source_record_version
  AND observation.source_record_hash = $source_record_hash
  AND ((observation.description IS NULL AND $description IS NULL)
    OR observation.description = $description)
  AND ((observation.observed_at IS NULL AND $observed_at IS NULL)
    OR observation.observed_at = datetime($observed_at))
  AND observation.available_at = datetime($available_at)
  AND observation.contract_version = $contract_version
MERGE (reference)-[:HAS_DESCRIPTION_OBSERVATION]->(observation)
WITH head, observation
OPTIONAL MATCH (head)-[prior:SELECTS_DESCRIPTION_OBSERVATION]->()
WITH head, observation, collect(prior) AS prior_links
FOREACH (link IN prior_links | DELETE link)
MERGE (head)-[:SELECTS_DESCRIPTION_OBSERVATION]->(observation)
RETURN observation.observation_id AS observation_id
"""

UPSERT_MEMBERSHIP_SNAPSHOT = """
MATCH (head:CrmCompanyMembershipHead {
  source_instance_id: $source_instance_id,
  subject_kind: $subject_kind,
  subject_id: $subject_id,
  control_instance_id: $control_instance_id,
  selected_snapshot_id: $snapshot_id
})
MERGE (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id})
ON CREATE SET snapshot.snapshot_digest = $snapshot_digest,
  snapshot.source_instance_id = $source_instance_id,
  snapshot.control_instance_id = $control_instance_id,
  snapshot.subject_kind = $subject_kind,
  snapshot.subject_id = $subject_id,
  snapshot.source_record_id = $source_record_id,
  snapshot.source_record_pk = $source_record_pk,
  snapshot.source_record_version = $source_record_version,
  snapshot.source_record_hash = $source_record_hash,
  snapshot.binding_count = $binding_count,
  snapshot.observed_at = CASE
    WHEN $observed_at IS NULL THEN null ELSE datetime($observed_at) END,
  snapshot.available_at = datetime($available_at),
  snapshot.contract_version = $contract_version,
  snapshot.created_at = datetime()
WITH head, snapshot
WHERE snapshot.snapshot_digest = $snapshot_digest
  AND snapshot.source_instance_id = $source_instance_id
  AND snapshot.control_instance_id = $control_instance_id
  AND snapshot.subject_kind = $subject_kind
  AND snapshot.subject_id = $subject_id
  AND snapshot.source_record_id = $source_record_id
  AND snapshot.source_record_pk = $source_record_pk
  AND snapshot.source_record_version = $source_record_version
  AND snapshot.source_record_hash = $source_record_hash
  AND snapshot.binding_count = $binding_count
  AND ((snapshot.observed_at IS NULL AND $observed_at IS NULL)
    OR snapshot.observed_at = datetime($observed_at))
  AND snapshot.available_at = datetime($available_at)
  AND snapshot.contract_version = $contract_version
OPTIONAL MATCH (head)-[prior:SELECTS_MEMBERSHIP_SNAPSHOT]->()
WITH head, snapshot, collect(prior) AS prior_links
FOREACH (link IN prior_links | DELETE link)
MERGE (head)-[:SELECTS_MEMBERSHIP_SNAPSHOT]->(snapshot)
RETURN snapshot.snapshot_id AS snapshot_id
"""

UPSERT_MEMBERSHIP_OBSERVATION = """
MATCH (snapshot:CrmCompanyMembershipSnapshot {
  snapshot_id: $snapshot_id,
  source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})
MATCH (reference:CrmCompanyReference {
  source_instance_id: $source_instance_id,
  company_id: $company_id,
  control_instance_id: $control_instance_id
})
MERGE (observation:CrmCompanyMembershipObservation {
  snapshot_id: $snapshot_id,
  company_id: $company_id
})
ON CREATE SET observation.observation_id = $observation_id,
  observation.subject_kind = $subject_kind,
  observation.subject_id = $subject_id,
  observation.sort = $sort,
  observation.role_id = $role_id,
  observation.is_primary = $is_primary,
  observation.created_at = datetime()
WITH snapshot, reference, observation
WHERE observation.observation_id = $observation_id
  AND observation.subject_kind = $subject_kind
  AND observation.subject_id = $subject_id
  AND ((observation.sort IS NULL AND $sort IS NULL) OR observation.sort = $sort)
  AND ((observation.role_id IS NULL AND $role_id IS NULL)
    OR observation.role_id = $role_id)
  AND observation.is_primary = $is_primary
MERGE (snapshot)-[:HAS_MEMBERSHIP_OBSERVATION]->(observation)
MERGE (observation)-[:REFERENCES_COMPANY]->(reference)
RETURN observation.observation_id AS observation_id
"""

VERIFY_COMPANY_REFERENCE = """
MATCH (reference:CrmCompanyReference {
  source_instance_id: $source_instance_id,
  company_id: $company_id,
  source_key: $source_key,
  control_instance_id: $control_instance_id,
  source_record_id: $source_record_id,
  identity_policy_version: $identity_policy_version,
  person_matching_prohibited: true
})
RETURN reference.company_id AS company_id
"""

VERIFY_DESCRIPTION_OBSERVATION = """
MATCH (reference:CrmCompanyReference {
  source_instance_id: $source_instance_id,
  company_id: $company_id,
  control_instance_id: $control_instance_id
})-[:HAS_DESCRIPTION_OBSERVATION]->
  (observation:CrmCompanyDescriptionObservation {observation_id: $observation_id})
MATCH (:CrmCompanyDescriptionHead {
  source_instance_id: $source_instance_id,
  company_id: $company_id,
  control_instance_id: $control_instance_id,
  selected_observation_id: $observation_id
})-[:SELECTS_DESCRIPTION_OBSERVATION]->(observation)
WHERE observation.observation_digest = $observation_digest
  AND observation.source_instance_id = $source_instance_id
  AND observation.control_instance_id = $control_instance_id
  AND observation.company_id = $company_id
  AND observation.source_record_id = $source_record_id
  AND observation.source_record_pk = $source_record_pk
  AND observation.source_record_version = $source_record_version
  AND observation.source_record_hash = $source_record_hash
  AND ((observation.description IS NULL AND $description IS NULL)
    OR observation.description = $description)
  AND ((observation.observed_at IS NULL AND $observed_at IS NULL)
    OR observation.observed_at = datetime($observed_at))
  AND observation.available_at = datetime($available_at)
  AND observation.contract_version = $contract_version
RETURN observation.observation_id AS observation_id
"""

VERIFY_MEMBERSHIP_SNAPSHOT = """
MATCH (head:CrmCompanyMembershipHead {
  source_instance_id: $source_instance_id,
  subject_kind: $subject_kind,
  subject_id: $subject_id,
  control_instance_id: $control_instance_id,
  selected_snapshot_id: $snapshot_id
})-[:SELECTS_MEMBERSHIP_SNAPSHOT]->
  (snapshot:CrmCompanyMembershipSnapshot {snapshot_id: $snapshot_id})
WHERE snapshot.snapshot_digest = $snapshot_digest
  AND snapshot.source_instance_id = $source_instance_id
  AND snapshot.control_instance_id = $control_instance_id
  AND snapshot.subject_kind = $subject_kind
  AND snapshot.subject_id = $subject_id
  AND snapshot.source_record_id = $source_record_id
  AND snapshot.source_record_pk = $source_record_pk
  AND snapshot.source_record_version = $source_record_version
  AND snapshot.source_record_hash = $source_record_hash
  AND snapshot.binding_count = $binding_count
  AND ((snapshot.observed_at IS NULL AND $observed_at IS NULL)
    OR snapshot.observed_at = datetime($observed_at))
  AND snapshot.available_at = datetime($available_at)
  AND snapshot.contract_version = $contract_version
RETURN snapshot.snapshot_id AS snapshot_id
"""

VERIFY_MEMBERSHIP_OBSERVATION = """
MATCH (snapshot:CrmCompanyMembershipSnapshot {
  snapshot_id: $snapshot_id,
  source_instance_id: $source_instance_id,
  control_instance_id: $control_instance_id
})-[:HAS_MEMBERSHIP_OBSERVATION]->
  (observation:CrmCompanyMembershipObservation {
    snapshot_id: $snapshot_id,
    company_id: $company_id,
    observation_id: $observation_id,
    subject_kind: $subject_kind,
    subject_id: $subject_id,
    is_primary: $is_primary
  })-[:REFERENCES_COMPANY]->
  (:CrmCompanyReference {
    source_instance_id: $source_instance_id,
    company_id: $company_id,
    control_instance_id: $control_instance_id
  })
WHERE ((observation.sort IS NULL AND $sort IS NULL) OR observation.sort = $sort)
  AND ((observation.role_id IS NULL AND $role_id IS NULL)
    OR observation.role_id = $role_id)
RETURN observation.observation_id AS observation_id
"""
