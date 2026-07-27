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
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failed:ProfileAnalysis {
    analysis_type: 'sales', status: 'failed', input_revision: input_revision
  })
  WITH failed ORDER BY failed.completed_at DESC, failed.analysis_id DESC
  RETURN head(collect(failed)) AS sales_failure
}
CALL (person, input_revision) {
  WITH person, input_revision
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failed:ProfileAnalysis {
    analysis_type: 'contact_tracing', status: 'failed', input_revision: input_revision
  })
  WITH failed ORDER BY failed.completed_at DESC, failed.analysis_id DESC
  RETURN head(collect(failed)) AS contact_failure
}
CALL (person) {
  WITH person
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'sales'
  })
  WHERE request.status = 'queued'
  RETURN count(request) > 0 AS sales_request_queued
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'contact_tracing'
  })
  WHERE request.status = 'queued'
  RETURN count(request) > 0 AS contact_request_queued
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'sales', status: 'running'
  })
  WHERE person.analysis_claim_until > now
  RETURN count(request) > 0 AS sales_request_running
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'contact_tracing', status: 'running'
  })
  WHERE person.analysis_claim_until > now
  RETURN count(request) > 0 AS contact_request_running
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'sales', force: true
  })
  WHERE request.requested_at > now - duration({hours: 1})
    AND request.status <> 'dispatch_failed'
  WITH request ORDER BY request.requested_at ASC
  RETURN count(request) AS sales_force_count, head(collect(request.requested_at)) AS sales_oldest_force
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'contact_tracing', force: true
  })
  WHERE request.requested_at > now - duration({hours: 1})
    AND request.status <> 'dispatch_failed'
  WITH request ORDER BY request.requested_at ASC
  RETURN count(request) AS contact_force_count, head(collect(request.requested_at)) AS contact_oldest_force
}
WITH input_revision, now, live_claim, sales_currents, contact_currents,
     sales_failure, contact_failure, sales_request_queued, contact_request_queued,
     sales_request_running, contact_request_running,
     sales_force_count, sales_oldest_force, contact_force_count, contact_oldest_force
RETURN input_revision,
       sales_request_running AS sales_claim_active,
       contact_request_running AS contact_claim_active,
       sales_request_queued,
       contact_request_queued,
       sales_currents,
       contact_currents,
       sales_failure {.analysis_id, .failure_code, .retryable, .next_retry_at}
         AS sales_failure,
       contact_failure {.analysis_id, .failure_code, .retryable, .next_retry_at}
         AS contact_failure,
       CASE WHEN sales_force_count >= 3 THEN 0 ELSE 3 - sales_force_count END
         AS sales_force_attempts_remaining,
       CASE WHEN sales_force_count >= 3 THEN sales_oldest_force + duration({hours: 1}) ELSE null END
         AS sales_force_available_at,
       CASE WHEN contact_force_count >= 3 THEN 0 ELSE 3 - contact_force_count END
         AS contact_force_attempts_remaining,
       CASE WHEN contact_force_count >= 3 THEN contact_oldest_force + duration({hours: 1}) ELSE null END
         AS contact_force_available_at
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

CREATE_PROFILE_ANALYSIS_REQUEST = (
    _RESOLVE_PROFILE_ANALYSIS_PERSON
    + """
WITH person, datetime.realtime() AS now
// Taking a write lock on the canonical Person serializes request creation and
// the rolling force-refresh budget for this Person/type.
SET person.profile_analysis_request_updated_at = now
WITH person, now, coalesce(person.analysis_input_revision, 0) AS input_revision
OPTIONAL MATCH (person)-[:CURRENT_PROFILE_ANALYSIS {analysis_type: $analysis_type}]
               ->(current:ProfileAnalysis)
WITH person, now, input_revision, head(collect(current)) AS current
OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(active:ProfileAnalysisRequest {
  analysis_type: $analysis_type
})
WHERE active.status = 'queued'
   OR (active.status = 'running' AND person.analysis_claim_until > now)
WITH person, now, input_revision, current, count(active) > 0 AS active_request
OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(forced:ProfileAnalysisRequest {
  analysis_type: $analysis_type, force: true
})
WHERE forced.requested_at > now - duration({hours: 1})
  AND forced.status <> 'dispatch_failed'
WITH person, now, input_revision, current, active_request,
     count(forced) AS force_count,
     head(collect(forced.requested_at)) AS oldest_force
WITH person, now, input_revision, active_request, force_count, oldest_force,
     current,
     current IS NOT NULL
       AND current.input_revision = input_revision
       AND current.completed_at + duration({hours: 24}) > now AS valid
WITH person, now, input_revision, active_request, force_count, oldest_force, valid,
     NOT active_request
       AND ($force OR NOT valid)
       AND (NOT $force OR force_count < 3) AS should_create
FOREACH (_ IN CASE WHEN should_create THEN [1] ELSE [] END |
  CREATE (request:ProfileAnalysisRequest {
    request_id: $request_id,
    person_id: person.person_id,
    analysis_type: $analysis_type,
    force: $force,
    status: 'queued',
    requested_at: now,
    input_revision: input_revision
  })
  CREATE (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request)
)
WITH person, now, active_request, force_count, oldest_force, valid, should_create
RETURN person.person_id AS person_id,
       CASE
         WHEN active_request THEN 'already_queued'
         WHEN NOT $force AND valid THEN 'already_valid'
         WHEN $force AND force_count >= 3 THEN 'force_limited'
         ELSE 'queued'
       END AS state,
       CASE WHEN should_create THEN $request_id ELSE null END AS request_id,
       CASE
         WHEN force_count + CASE WHEN should_create AND $force THEN 1 ELSE 0 END >= 3 THEN 0
         ELSE 3 - force_count - CASE WHEN should_create AND $force THEN 1 ELSE 0 END
       END AS force_attempts_remaining,
       CASE
         WHEN force_count >= 3 THEN oldest_force + duration({hours: 1})
         WHEN force_count = 2 AND should_create AND $force THEN now + duration({hours: 1})
         ELSE null
       END AS force_available_at
"""
)

MARK_PROFILE_ANALYSIS_REQUEST_DISPATCH_FAILED = """
MATCH (:Person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {request_id: $request_id})
WHERE request.status = 'queued'
SET request.status = 'dispatch_failed', request.completed_at = datetime.realtime()
RETURN true AS updated
"""
