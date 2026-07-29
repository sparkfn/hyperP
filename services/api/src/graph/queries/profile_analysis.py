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
CALL (person, input_revision, sales_currents) {
  WITH person, input_revision, head(sales_currents) AS current
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failed:ProfileAnalysis {
    analysis_type: 'sales', status: 'failed', input_revision: input_revision
  })
  WHERE current IS NULL
    OR failed.completed_at > current.completed_at
    OR (
      failed.completed_at = current.completed_at
      AND failed.analysis_id > current.analysis_id
    )
  WITH failed ORDER BY failed.completed_at DESC, failed.analysis_id DESC
  RETURN head(collect(failed)) AS sales_failure
}
CALL (person, input_revision, contact_currents) {
  WITH person, input_revision, head(contact_currents) AS current
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failed:ProfileAnalysis {
    analysis_type: 'contact_tracing', status: 'failed', input_revision: input_revision
  })
  WHERE current IS NULL
    OR failed.completed_at > current.completed_at
    OR (
      failed.completed_at = current.completed_at
      AND failed.analysis_id > current.analysis_id
    )
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
  WITH request ORDER BY request.requested_at ASC
  RETURN count(request) AS sales_force_count, head(collect(request.requested_at)) AS sales_oldest_force
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
    analysis_type: 'contact_tracing', force: true
  })
  WHERE request.requested_at > now - duration({hours: 1})
  WITH request ORDER BY request.requested_at ASC
  RETURN count(request) AS contact_force_count, head(collect(request.requested_at)) AS contact_oldest_force
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(retry:ProfileAnalysisRequest {
    user_retry: true, retry_actor_id: $retry_actor_id
  })
  WHERE retry.requested_at > now - duration({hours: 1})
  WITH retry ORDER BY retry.requested_at ASC
  RETURN count(retry) AS user_retry_count,
         head(collect(retry.requested_at)) AS user_oldest_retry
}
WITH input_revision, now, live_claim, sales_currents, contact_currents,
     sales_failure, contact_failure, sales_request_queued, contact_request_queued,
     sales_request_running, contact_request_running,
     sales_force_count, sales_oldest_force, contact_force_count, contact_oldest_force,
     user_retry_count, user_oldest_retry
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
       CASE WHEN user_retry_count >= $max_user_retries
         THEN 0 ELSE $max_user_retries - user_retry_count END
         AS retry_attempts_remaining,
       CASE WHEN user_retry_count >= $max_user_retries
         THEN user_oldest_retry + duration({hours: 1}) ELSE null END
         AS retry_available_at,
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
CALL (person) {
  WITH person
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(active:ProfileAnalysisRequest {
    analysis_type: $analysis_type
  })
  WHERE active.status IN ['queued', 'running']
  WITH active
  ORDER BY CASE active.status WHEN 'queued' THEN 0 ELSE 1 END,
           active.requested_at,
           active.request_id
  RETURN head(collect(active)) AS active_request
}
WITH person, now, input_revision, current, active_request
OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(forced:ProfileAnalysisRequest {
  analysis_type: $analysis_type, force: true
})
WHERE forced.requested_at > now - duration({hours: 1})
WITH person, now, input_revision, current, active_request, forced
ORDER BY forced.requested_at ASC
WITH person, now, input_revision, current, active_request,
     count(forced) AS force_count,
     head(collect(forced.requested_at)) AS oldest_force
WITH person, now, input_revision, active_request, force_count, oldest_force,
     current,
     current IS NOT NULL
       AND current.input_revision = input_revision
       AND current.completed_at + duration({hours: 24}) > now AS valid
WITH person, now, input_revision, active_request, force_count, oldest_force, valid,
     active_request IS NULL
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
         WHEN active_request IS NOT NULL THEN 'already_queued'
         WHEN NOT $force AND valid THEN 'already_valid'
         WHEN $force AND force_count >= 3 THEN 'force_limited'
         ELSE 'queued'
       END AS state,
       CASE
         WHEN should_create THEN $request_id
         WHEN active_request.status = 'queued' THEN active_request.request_id
         WHEN active_request.status = 'running'
           AND (person.analysis_claim_until IS NULL OR person.analysis_claim_until <= now)
           THEN active_request.request_id
         ELSE null
       END AS request_id,
       CASE
         WHEN force_count + CASE WHEN should_create AND $force THEN 1 ELSE 0 END >= 3 THEN 0
         ELSE 3 - force_count - CASE WHEN should_create AND $force THEN 1 ELSE 0 END
       END AS force_attempts_remaining,
       CASE
         WHEN force_count >= 3 THEN oldest_force + duration({hours: 1})
         WHEN force_count = 2 AND should_create AND $force
           THEN oldest_force + duration({hours: 1})
         ELSE null
       END AS force_available_at
"""
)

CREATE_FAILED_PROFILE_ANALYSIS_RETRY = (
    _RESOLVE_PROFILE_ANALYSIS_PERSON
    + """
WITH person, datetime.realtime() AS now
// Lock the canonical Person so concurrent retries share one rolling budget.
SET person.profile_analysis_request_updated_at = now
WITH person, now, coalesce(person.analysis_input_revision, 0) AS input_revision
CALL (person, input_revision) {
  WITH person, input_revision
  OPTIONAL MATCH (person)-[:CURRENT_PROFILE_ANALYSIS {analysis_type: $analysis_type}]
                 ->(current:ProfileAnalysis)
  WITH person, input_revision, head(collect(current)) AS current
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failure:ProfileAnalysis {
    analysis_type: $analysis_type,
    input_revision: input_revision,
    status: 'failed'
  })
  WHERE current IS NULL
    OR failure.completed_at > current.completed_at
    OR (
      failure.completed_at = current.completed_at
      AND failure.analysis_id > current.analysis_id
    )
  WITH failure ORDER BY failure.completed_at DESC, failure.analysis_id DESC
  RETURN head(collect(failure)) AS failure
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(active:ProfileAnalysisRequest {
    analysis_type: $analysis_type
  })
  WHERE active.status IN ['queued', 'running']
  WITH active
  ORDER BY CASE active.status WHEN 'queued' THEN 0 ELSE 1 END,
           active.requested_at,
           active.request_id
  RETURN head(collect(active)) AS active_request
}
CALL (person, now) {
  WITH person, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(retry:ProfileAnalysisRequest {
    user_retry: true, retry_actor_id: $retry_actor_id
  })
  WHERE retry.requested_at > now - duration({hours: 1})
  WITH retry ORDER BY retry.requested_at ASC
  RETURN count(retry) AS retry_count,
         head(collect(retry.requested_at)) AS oldest_retry
}
WITH person, now, input_revision, failure, active_request, retry_count, oldest_retry,
     CASE
       WHEN failure IS NULL THEN 'not_failed'
       WHEN active_request IS NOT NULL THEN 'already_active'
       WHEN retry_count >= $max_retries THEN 'retry_limited'
       ELSE 'queued'
     END AS state
FOREACH (_ IN CASE WHEN state = 'queued' THEN [1] ELSE [] END |
  CREATE (request:ProfileAnalysisRequest {
    request_id: $request_id,
    person_id: person.person_id,
    analysis_type: $analysis_type,
    force: false,
    status: 'queued',
    requested_at: now,
    input_revision: input_revision,
    user_retry: true,
    retry_actor_id: $retry_actor_id,
    retry_of_analysis_id: failure.analysis_id
  })
  CREATE (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request)
)
WITH person, now, state, active_request, retry_count, oldest_retry,
     CASE WHEN state = 'queued' THEN 1 ELSE 0 END AS created_count
RETURN person.person_id AS person_id,
       CASE
         WHEN state = 'queued' THEN $request_id
         WHEN state = 'already_active' AND active_request.status = 'queued'
           THEN active_request.request_id
         WHEN state = 'already_active' AND active_request.status = 'running'
           AND (person.analysis_claim_until IS NULL OR person.analysis_claim_until <= now)
           THEN active_request.request_id
         ELSE null
       END AS request_id,
       state,
       CASE WHEN retry_count + created_count >= $max_retries
         THEN 0 ELSE $max_retries - retry_count - created_count END
         AS retry_attempts_remaining,
       CASE WHEN retry_count + created_count >= $max_retries
         THEN coalesce(oldest_retry, now) + duration({hours: 1}) ELSE null END
         AS retry_available_at
"""
)

REQUEUE_FAILED_PROFILE_ANALYSIS_REQUEST = (
    _RESOLVE_PROFILE_ANALYSIS_PERSON
    + """
WITH person, datetime.realtime() AS now, coalesce(person.analysis_input_revision, 0) AS input_revision
OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
  request_id: $request_id
})
WITH person, now, input_revision, head(collect(request)) AS request
CALL (person, request, now) {
  WITH person, request, now
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(active:ProfileAnalysisRequest)
  WHERE request IS NOT NULL
    AND active.analysis_type = request.analysis_type
    AND active.status IN ['queued', 'running']
    AND active.request_id <> request.request_id
  RETURN count(active) > 0 AS active_request
}
CALL (person, request) {
  WITH person, request
  OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(failure:ProfileAnalysis {
    analysis_type: request.analysis_type,
    input_revision: request.input_revision,
    status: 'failed'
  })
  WITH failure ORDER BY failure.completed_at DESC, failure.analysis_id DESC
  RETURN head(collect(failure)) AS failure
}
WITH person, now, input_revision, request, active_request, failure,
     CASE
       WHEN request IS NULL THEN 'request_not_found'
       WHEN request.status <> 'failed' THEN 'not_terminal'
       WHEN request.input_revision IS NULL OR request.input_revision <> input_revision
         THEN 'revision_conflict'
       WHEN active_request THEN 'already_active'
       WHEN failure IS NULL
         OR coalesce(failure.failure_code, '') NOT IN [
           'invalid_snapshot',
           'invalid_snapshot_temporal',
           'invalid_output',
           'provider_rejected'
         ]
         OR coalesce(failure.retryable, false) THEN 'nonrecoverable'
       WHEN coalesce(failure.attempt_number, $max_attempts) >= $max_attempts
         THEN 'attempt_limited'
       WHEN coalesce(request.operator_requeue_count, 0) >= 1 THEN 'requeue_limited'
       ELSE 'requeued'
     END AS state
FOREACH (_ IN CASE WHEN state = 'requeued' THEN [1] ELSE [] END |
  SET request.status = 'queued',
      request.completed_at = null,
      request.started_at = null,
      request.next_retry_at = null,
      request.requeued_at = now,
      request.operator_requeue_count = coalesce(request.operator_requeue_count, 0) + 1
)
RETURN person.person_id AS person_id,
       request.request_id AS request_id,
       request.analysis_type AS analysis_type,
       state
"""
)
