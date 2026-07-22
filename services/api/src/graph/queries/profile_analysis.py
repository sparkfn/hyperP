"""Safe read projections for Person profile-analysis current state and history."""

from __future__ import annotations

_RESOLVE_PROFILE_ANALYSIS_PERSON = """
MATCH (requested:Person {person_id: $person_id})
OPTIONAL MATCH (requested)-[:MERGED_INTO]->(canonical:Person)
WITH coalesce(canonical, requested) AS person
"""

_SAFE_CURRENT_PROJECTION = """{
  .analysis_id, .person_id, .analysis_type, .status, .content,
  .input_revision, .input_fingerprint, .prompt_version, .provider, .model,
  .started_at, .completed_at, .attempt_number
}"""

_SAFE_HISTORY_PROJECTION = """{
  .analysis_id, .person_id, .analysis_type, .status, .content,
  .input_revision, .input_fingerprint, .prompt_version, .provider, .model,
  .started_at, .completed_at, .attempt_number,
  .failure_code, .retryable, .next_retry_at
}"""

GET_PERSON_PROFILE_ANALYSES = (
    _RESOLVE_PROFILE_ANALYSIS_PERSON
    + """
WITH person,
     coalesce(person.analysis_input_revision, 0) AS input_revision,
     datetime.realtime() AS now
WITH person, input_revision, now,
     coalesce(person.analysis_claim_until > now, false) AS live_claim
CALL (person) {
  WITH person
  OPTIONAL MATCH (person)-[:CURRENT_PROFILE_ANALYSIS {analysis_type: 'sales'}]
                 ->(sales:ProfileAnalysis)
  RETURN collect(sales """
    + _SAFE_CURRENT_PROJECTION
    + """) AS sales_currents
}
CALL (person) {
  WITH person
  OPTIONAL MATCH (person)-[:CURRENT_PROFILE_ANALYSIS {analysis_type: 'contact_tracing'}]
                 ->(contact:ProfileAnalysis)
  RETURN collect(contact """
    + _SAFE_CURRENT_PROJECTION
    + """) AS contact_currents
}
CALL (person, input_revision) {
  WITH person, input_revision
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failure:ProfileAnalysis {
    analysis_type: 'sales', status: 'failed', input_revision: input_revision
  })
  WITH failure ORDER BY failure.completed_at DESC, failure.analysis_id DESC
  RETURN head(collect(failure)) AS sales_failure
}
CALL (person, input_revision) {
  WITH person, input_revision
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failure:ProfileAnalysis {
    analysis_type: 'contact_tracing', status: 'failed', input_revision: input_revision
  })
  WITH failure ORDER BY failure.completed_at DESC, failure.analysis_id DESC
  RETURN head(collect(failure)) AS contact_failure
}
WITH input_revision, now, live_claim, sales_currents, contact_currents,
     sales_failure, contact_failure,
     coalesce(live_claim AND (
       sales_failure IS NULL OR (
         sales_failure.retryable = true AND sales_failure.next_retry_at <= now
       )
     ), false) AS sales_claim_active,
     coalesce(live_claim AND (
       contact_failure IS NULL OR (
         contact_failure.retryable = true AND contact_failure.next_retry_at <= now
       )
     ), false) AS contact_claim_active
RETURN input_revision,
       sales_claim_active,
       contact_claim_active,
       sales_currents,
       contact_currents,
       sales_failure {.analysis_id, .failure_code, .retryable, .next_retry_at}
         AS sales_failure,
       contact_failure {.analysis_id, .failure_code, .retryable, .next_retry_at}
         AS contact_failure
"""
)

GET_PERSON_PROFILE_ANALYSIS_HISTORY = (
    _RESOLVE_PROFILE_ANALYSIS_PERSON
    + """
CALL (person) {
  WITH person
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(analysis:ProfileAnalysis)
  WHERE analysis.status IN ['succeeded', 'failed', 'obsolete']
    AND ($analysis_type IS NULL OR analysis.analysis_type = $analysis_type)
  RETURN count(analysis) AS total
}
CALL (person) {
  WITH person
  MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(analysis:ProfileAnalysis)
  WHERE analysis.status IN ['succeeded', 'failed', 'obsolete']
    AND ($analysis_type IS NULL OR analysis.analysis_type = $analysis_type)
  WITH analysis
  ORDER BY analysis.completed_at DESC, analysis.analysis_id DESC
  SKIP $skip LIMIT $limit
  RETURN collect(analysis """
    + _SAFE_HISTORY_PROJECTION
    + """) AS analyses
}
RETURN person.person_id AS person_id,
       total,
       analyses
"""
)
