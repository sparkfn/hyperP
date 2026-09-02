"""Cypher primitives for immutable ProfileAnalysis history and publication."""

from __future__ import annotations

RELEASE_PROFILE_ANALYSIS_CLAIM = """
MATCH (person:Person {person_id: $person_id})
WHERE person.analysis_claim_token = $claim_token
SET person.analysis_claim_token = null,
    person.analysis_claim_until = null
RETURN true AS released
"""


RENEW_PROFILE_ANALYSIS_CLAIM = """
MATCH (person:Person {person_id: $person_id})
WHERE person.analysis_claim_token = $claim_token
  AND person.status = 'active'
  AND coalesce(person.analysis_input_revision, 0) = $input_revision
SET person.analysis_claim_until = $claim_until
RETURN true AS renewed
"""


FETCH_PROFILE_ANALYSIS_SNAPSHOT_ROWS = """
MATCH (person:Person {person_id: $person_id})
CALL (person) {
  RETURN 'profile' AS row_kind,
         person.person_id AS internal_id,
         null AS parent_internal_id,
         // Exact DOB is not read until a safe normalized date contract is available.
         null AS age_band,
         CASE
           WHEN person.profile_completeness_score IS NULL THEN 'unknown'
           WHEN person.profile_completeness_score < 0.34 THEN 'low'
           WHEN person.profile_completeness_score < 0.67 THEN 'medium'
           ELSE 'high'
         END AS completeness_band,
         person.profile_completeness_score AS completeness_score,
         null AS record_type, null AS source_category, null AS observed_date,
         null AS quality_flag, null AS trust_tier, null AS confidence,
         null AS order_date, null AS total, null AS currency, null AS merchant,
         null AS product, null AS category, null AS manufacturer, null AS model,
         null AS relationship_category, null AS direction, null AS event_date,
         null AS omitted_sources, null AS omitted_orders,
         null AS omitted_order_items, null AS omitted_vehicles,
         null AS omitted_relationships
  UNION ALL
  WITH person
  CALL (person) {
    MATCH (source:SourceRecord)-[source_link:LINKED_TO]->(person)
    WHERE coalesce(source_link.is_active, true) = true
      AND source.lifecycle_status = 'active'
      AND (source.history_family IS NULL OR source.history_family = 'activity')
    RETURN count(DISTINCT source) AS total_sources
  }
  CALL (person) {
    MATCH (person)-[purchase:PURCHASED]->(order:Order)
    WHERE coalesce(purchase.is_active, true) = true
    RETURN count(DISTINCT order) AS total_orders
  }
  CALL (person) {
    MATCH (person)-[purchase:PURCHASED]->(order:Order)
    WHERE coalesce(purchase.is_active, true) = true
    MATCH (order)-[:CONTAINS]->(:LineItem)-[:OF_PRODUCT]->(item:Product)
    WITH DISTINCT order.order_id AS order_id,
         coalesce(item.display_name, item.name) AS product,
         item.category AS category
    RETURN count(*) AS total_order_items
  }
  CALL (person) {
    MATCH (person)-[purchase:PURCHASED]->(order:Order)
    WHERE coalesce(purchase.is_active, true) = true
    WITH DISTINCT order
    ORDER BY order.ordered_at DESC, order.order_id DESC
    LIMIT 8
    CALL (order) {
      MATCH (order)-[:CONTAINS]->(:LineItem)-[:OF_PRODUCT]->(item:Product)
      WITH DISTINCT coalesce(item.display_name, item.name) AS product,
           item.category AS category
      ORDER BY product, category
      LIMIT 5
      RETURN count(*) AS retained_items
    }
    RETURN coalesce(sum(retained_items), 0) AS retained_order_items
  }
  CALL (person) {
    CALL (person) {
      MATCH (person)-[vehicle_link:OWNS_VEHICLE|BOUGHT_VEHICLE]->(vehicle:Vehicle)
      WHERE coalesce(vehicle_link.is_active, true) = true
      RETURN vehicle.vehicle_id AS vehicle_id, type(vehicle_link) AS relationship_type
      UNION ALL
      WITH person
      MATCH (source:SourceRecord)-[source_link:LINKED_TO]->(person)
      MATCH (source)-[vehicle_link:MENTIONS_VEHICLE]->(vehicle:Vehicle)
      WHERE coalesce(source_link.is_active, true) = true
        AND source.lifecycle_status = 'active'
        AND (source.history_family IS NULL OR source.history_family = 'activity')
        AND coalesce(vehicle_link.is_active, true) = true
      RETURN DISTINCT vehicle.vehicle_id AS vehicle_id,
             'MENTIONS_VEHICLE' AS relationship_type
    }
    RETURN count(*) AS total_vehicles
  }
  CALL (person) {
    MATCH (person)-[relationship:KNOWS]-(related:Person {status: 'active'})
    WHERE coalesce(relationship.is_active, true) = true
    RETURN count(relationship) AS total_relationships
  }
  RETURN 'counts' AS row_kind,
         'counts' AS internal_id,
         null AS parent_internal_id,
         null AS age_band, null AS completeness_band, null AS completeness_score,
         null AS record_type, null AS source_category, null AS observed_date,
         null AS quality_flag, null AS trust_tier, null AS confidence,
         null AS order_date, null AS total, null AS currency, null AS merchant,
         null AS product, null AS category, null AS manufacturer, null AS model,
         null AS relationship_category, null AS direction, null AS event_date,
         CASE WHEN total_sources > 20 THEN total_sources - 20 ELSE 0 END
           AS omitted_sources,
         CASE WHEN total_orders > 8 THEN total_orders - 8 ELSE 0 END
           AS omitted_orders,
         total_order_items - retained_order_items AS omitted_order_items,
         CASE WHEN total_vehicles > 10 THEN total_vehicles - 10 ELSE 0 END
           AS omitted_vehicles,
         CASE WHEN total_relationships > 20 THEN total_relationships - 20 ELSE 0 END
           AS omitted_relationships
  UNION ALL
  WITH person
  MATCH (source:SourceRecord)-[source_link:LINKED_TO]->(person)
  WHERE coalesce(source_link.is_active, true) = true
    AND source.lifecycle_status = 'active'
    AND (source.history_family IS NULL OR source.history_family = 'activity')
  WITH person, source
  ORDER BY source.observed_at DESC, source.source_record_pk DESC
  LIMIT 20
  OPTIONAL MATCH (person)-[fact:HAS_FACT]->(source)
  WHERE coalesce(fact.is_active, true) = true
  WITH source,
       [value IN collect(DISTINCT fact.quality_flag) WHERE value IS NOT NULL]
         AS quality_flags,
       [value IN collect(DISTINCT fact.source_trust_tier) WHERE value IS NOT NULL]
         AS trust_tiers,
       [value IN collect(DISTINCT fact.confidence) + [source.extraction_confidence]
        WHERE value IS NOT NULL] AS confidence_values
  RETURN 'source' AS row_kind,
         source.source_record_pk AS internal_id,
         null AS parent_internal_id,
         null AS age_band, null AS completeness_band, null AS completeness_score,
         source.record_type AS record_type,
         source.record_type AS source_category,
         CASE
           WHEN source.observed_at IS NULL THEN null
           WHEN valueType(source.observed_at) STARTS WITH 'STRING'
             THEN toString(source.observed_at)
           WHEN valueType(source.observed_at) STARTS WITH 'DATE'
             OR valueType(source.observed_at) STARTS WITH 'LOCAL DATETIME'
             OR valueType(source.observed_at) STARTS WITH 'ZONED DATETIME'
             THEN toString(date(source.observed_at))
           ELSE 'invalid'
         END AS observed_date,
         CASE
           WHEN any(value IN quality_flags WHERE value = 'source_untrusted')
             THEN 'source_untrusted'
           WHEN any(value IN quality_flags WHERE value = 'invalid_format')
             THEN 'invalid_format'
           WHEN any(value IN quality_flags WHERE value = 'placeholder_value')
             THEN 'placeholder_value'
           WHEN any(value IN quality_flags WHERE value = 'shared_suspected')
             THEN 'shared_suspected'
           WHEN any(value IN quality_flags WHERE value = 'stale') THEN 'stale'
           WHEN any(value IN quality_flags WHERE value = 'partial_parse')
             THEN 'partial_parse'
           WHEN size(quality_flags) > 0
             AND all(value IN quality_flags WHERE value = 'valid') THEN 'valid'
           ELSE null
         END AS quality_flag,
         CASE
           WHEN 'tier_4' IN trust_tiers THEN 'tier_4'
           WHEN 'tier_3' IN trust_tiers THEN 'tier_3'
           WHEN 'tier_2' IN trust_tiers THEN 'tier_2'
           WHEN 'tier_1' IN trust_tiers THEN 'tier_1'
           ELSE null
         END AS trust_tier,
         reduce(minimum = null, value IN confidence_values |
           CASE WHEN minimum IS NULL OR value < minimum THEN value ELSE minimum END
         ) AS confidence,
         null AS order_date, null AS total, null AS currency, null AS merchant,
         null AS product, null AS category, null AS manufacturer, null AS model,
         null AS relationship_category, null AS direction, null AS event_date,
         null AS omitted_sources, null AS omitted_orders,
         null AS omitted_order_items, null AS omitted_vehicles,
         null AS omitted_relationships
  UNION ALL
  WITH person
  MATCH (person)-[purchase:PURCHASED]->(order:Order)
  WHERE coalesce(purchase.is_active, true) = true
  WITH DISTINCT order
  ORDER BY order.ordered_at DESC, order.order_id DESC
  LIMIT 8
  CALL (order) {
    OPTIONAL MATCH (order)-[:CONTAINS]->(:LineItem)-[:OF_PRODUCT]->(item:Product)
    WITH DISTINCT coalesce(item.display_name, item.name) AS product,
         item.category AS category
    ORDER BY product, category
    LIMIT 5
    RETURN product, category
  }
  RETURN 'order' AS row_kind,
         order.order_id AS internal_id,
         null AS parent_internal_id,
         null AS age_band, null AS completeness_band, null AS completeness_score,
         null AS record_type, null AS source_category, null AS observed_date,
         null AS quality_flag, null AS trust_tier, null AS confidence,
         CASE
           WHEN order.ordered_at IS NULL THEN null
           WHEN valueType(order.ordered_at) STARTS WITH 'STRING'
             THEN toString(order.ordered_at)
           WHEN valueType(order.ordered_at) STARTS WITH 'DATE'
             OR valueType(order.ordered_at) STARTS WITH 'LOCAL DATETIME'
             OR valueType(order.ordered_at) STARTS WITH 'ZONED DATETIME'
             THEN toString(date(order.ordered_at))
           ELSE 'invalid'
         END AS order_date,
         order.total_amount AS total,
         order.currency AS currency,
         null AS merchant,
         product, category,
         null AS manufacturer, null AS model,
         null AS relationship_category, null AS direction, null AS event_date,
         null AS omitted_sources, null AS omitted_orders,
         null AS omitted_order_items, null AS omitted_vehicles,
         null AS omitted_relationships
  UNION ALL
  WITH person
  CALL (person) {
    MATCH (person)-[vehicle_link:OWNS_VEHICLE|BOUGHT_VEHICLE]->(vehicle:Vehicle)
    WHERE coalesce(vehicle_link.is_active, true) = true
    RETURN vehicle,
           CASE type(vehicle_link)
             WHEN 'OWNS_VEHICLE' THEN 'owned'
             WHEN 'BOUGHT_VEHICLE' THEN 'purchased'
             ELSE 'other'
           END AS relationship_category
    UNION ALL
    WITH person
    MATCH (source:SourceRecord)-[source_link:LINKED_TO]->(person)
    MATCH (source)-[vehicle_link:MENTIONS_VEHICLE]->(vehicle:Vehicle)
    WHERE coalesce(source_link.is_active, true) = true
      AND source.lifecycle_status = 'active'
      AND (source.history_family IS NULL OR source.history_family = 'activity')
      AND coalesce(vehicle_link.is_active, true) = true
    RETURN DISTINCT vehicle, 'inquired' AS relationship_category
  }
  WITH vehicle, relationship_category
  ORDER BY vehicle.product, vehicle.manufacturer, vehicle.model,
           relationship_category, vehicle.vehicle_id
  LIMIT 10
  RETURN 'vehicle' AS row_kind,
         vehicle.vehicle_id AS internal_id,
         null AS parent_internal_id,
         null AS age_band, null AS completeness_band, null AS completeness_score,
         null AS record_type, null AS source_category, null AS observed_date,
         null AS quality_flag, null AS trust_tier, null AS confidence,
         null AS order_date, null AS total, null AS currency, null AS merchant,
         vehicle.product AS product, vehicle.category AS category,
         vehicle.manufacturer AS manufacturer, vehicle.model AS model,
         relationship_category,
         null AS direction, null AS event_date,
         null AS omitted_sources, null AS omitted_orders,
         null AS omitted_order_items, null AS omitted_vehicles,
         null AS omitted_relationships
  UNION ALL
  WITH person
  MATCH (person)-[relationship:KNOWS]-(related:Person {status: 'active'})
  WHERE coalesce(relationship.is_active, true) = true
  WITH person, relationship, related
  ORDER BY relationship.last_confirmed_at DESC, relationship.knows_id DESC
  LIMIT 20
  RETURN 'relationship' AS row_kind,
         relationship.knows_id AS internal_id,
         related.person_id AS parent_internal_id,
         null AS age_band, null AS completeness_band, null AS completeness_score,
         null AS record_type, null AS source_category, null AS observed_date,
         null AS quality_flag, null AS trust_tier, null AS confidence,
         null AS order_date, null AS total, null AS currency, null AS merchant,
         null AS product, null AS category, null AS manufacturer, null AS model,
         coalesce(relationship.relationship_category, 'other')
           AS relationship_category,
         CASE WHEN startNode(relationship) = person THEN 'outgoing' ELSE 'incoming' END
           AS direction,
         CASE
           WHEN relationship.last_confirmed_at IS NULL THEN null
           WHEN valueType(relationship.last_confirmed_at) STARTS WITH 'STRING'
             THEN toString(relationship.last_confirmed_at)
           WHEN valueType(relationship.last_confirmed_at) STARTS WITH 'DATE'
             OR valueType(relationship.last_confirmed_at) STARTS WITH 'LOCAL DATETIME'
             OR valueType(relationship.last_confirmed_at) STARTS WITH 'ZONED DATETIME'
             THEN toString(date(relationship.last_confirmed_at))
           ELSE 'invalid'
         END AS event_date,
         null AS omitted_sources, null AS omitted_orders,
         null AS omitted_order_items, null AS omitted_vehicles,
         null AS omitted_relationships
}
RETURN row_kind, internal_id, parent_internal_id,
       age_band, completeness_band, completeness_score,
       record_type, source_category, observed_date, quality_flag, trust_tier, confidence,
       order_date, total, currency, merchant, product, category, manufacturer, model,
       relationship_category, direction, event_date,
       omitted_sources, omitted_orders, omitted_order_items,
       omitted_vehicles, omitted_relationships
ORDER BY row_kind, internal_id, product, category
"""


FETCH_PROFILE_ANALYSIS_SENSITIVE_VALUES = """
MATCH (person:Person {person_id: $person_id})
OPTIONAL MATCH (person)-[identifier_link:IDENTIFIED_BY]->(identifier:Identifier)
WHERE coalesce(identifier_link.is_active, true) = true
OPTIONAL MATCH (person)-[fact:HAS_FACT]->(:SourceRecord)
WHERE coalesce(fact.is_active, true) = true
  AND fact.attribute_name IN ['full_name', 'preferred_name', 'legal_name', 'dob']
OPTIONAL MATCH (person)-[address_link:LIVES_AT]->(address:Address)
WHERE coalesce(address_link.is_active, true) = true
WITH person,
     [value IN collect(DISTINCT identifier.normalized_value)
      WHERE value IS NOT NULL | toString(value)] AS identifier_values,
     [value IN collect(DISTINCT fact.attribute_value)
      WHERE value IS NOT NULL | toString(value)] AS fact_values,
     [value IN collect(DISTINCT address.normalized_full)
      WHERE value IS NOT NULL | toString(value)] AS address_values,
     [value IN [person.preferred_full_name, person.preferred_phone,
                person.preferred_email, person.preferred_dob,
                person.preferred_nric, person.preferred_address_id]
      WHERE value IS NOT NULL | toString(value)] AS direct_values
RETURN identifier_values + fact_values + address_values + direct_values
       AS known_sensitive_values
"""


PERSIST_PROFILE_ANALYSIS_ATTEMPT = """
MATCH (person:Person {person_id: $person_id})
WHERE $status IN ['failed', 'obsolete']
SET person.analysis_last_attempt_at = CASE
    WHEN person.analysis_last_attempt_at IS NULL
      OR person.analysis_last_attempt_at < $completed_at
    THEN $completed_at
    ELSE person.analysis_last_attempt_at
END
WITH person,
     person.analysis_claim_token = $claim_token
     AND person.analysis_claim_until > datetime.realtime()
     AND person.status = 'active'
     AND coalesce(person.analysis_input_revision, 0) = $input_revision AS claim_owned
WITH person,
     CASE WHEN claim_owned THEN $status ELSE 'obsolete' END AS actual_status
CREATE (analysis:ProfileAnalysis {
    analysis_id: $analysis_id,
    person_id: $person_id,
    analysis_type: $analysis_type,
    status: actual_status,
    input_revision: $input_revision,
    input_fingerprint: $input_fingerprint,
    prompt_version: $prompt_version,
    provider: $provider,
    model: $model,
    started_at: $started_at,
    completed_at: $completed_at,
    failure_code: CASE WHEN actual_status = 'failed' THEN $failure_code ELSE null END,
    retryable: CASE WHEN actual_status = 'failed' THEN $retryable ELSE null END,
    next_retry_at: CASE WHEN actual_status = 'failed' THEN $next_retry_at ELSE null END,
    attempt_number: $attempt_number
})
CREATE (person)-[:HAS_PROFILE_ANALYSIS]->(analysis)
RETURN analysis.analysis_id AS analysis_id,
       analysis.status AS status,
       false AS publishable
"""


PERSIST_AND_PUBLISH_PROFILE_ANALYSIS_SUCCESS = """
MATCH (person:Person {person_id: $person_id})
SET person.analysis_last_attempt_at = CASE
    WHEN person.analysis_last_attempt_at IS NULL
      OR person.analysis_last_attempt_at < $completed_at
    THEN $completed_at
    ELSE person.analysis_last_attempt_at
END
WITH person,
     person.analysis_claim_token = $claim_token
     AND person.analysis_claim_until > datetime.realtime()
     AND person.status = 'active'
     AND coalesce(person.analysis_input_revision, 0) = $input_revision AS publishable
CREATE (analysis:ProfileAnalysis {
    analysis_id: $analysis_id,
    person_id: $person_id,
    analysis_type: $analysis_type,
    status: CASE WHEN publishable THEN 'succeeded' ELSE 'obsolete' END,
    content: $content,
    input_revision: $input_revision,
    input_fingerprint: $input_fingerprint,
    prompt_version: $prompt_version,
    provider: $provider,
    model: $model,
    started_at: $started_at,
    completed_at: $completed_at,
    attempt_number: $attempt_number
})
CREATE (person)-[:HAS_PROFILE_ANALYSIS]->(analysis)
WITH person, analysis, publishable
OPTIONAL MATCH (person)-[current:CURRENT_PROFILE_ANALYSIS {
    analysis_type: $analysis_type
}]->(:ProfileAnalysis)
WHERE publishable
FOREACH (relationship IN CASE WHEN current IS NULL THEN [] ELSE [current] END |
    DELETE relationship
)
WITH DISTINCT person, analysis, publishable
FOREACH (_ IN CASE WHEN publishable THEN [1] ELSE [] END |
    CREATE (person)-[:CURRENT_PROFILE_ANALYSIS {
        analysis_type: $analysis_type
    }]->(analysis)
)
RETURN analysis.analysis_id AS analysis_id,
       analysis.status AS status,
       publishable
"""

CLAIM_PROFILE_ANALYSIS_REQUEST = """
MATCH (person:Person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
  request_id: $request_id
})
WHERE person.status = 'active'
// Acquire the Person write lock before revalidating request and lease state.
SET person.analysis_claim_until = coalesce(
  person.analysis_claim_until,
  datetime({epochMillis: 0})
)
WITH person, request
WHERE request.status IN ['queued', 'running']
  AND person.analysis_claim_until <= $now
WITH person, request, coalesce(person.analysis_input_revision, 0) AS input_revision
SET person.analysis_claim_token = $claim_token,
    person.analysis_claim_until = $claim_until,
    request.status = 'running',
    request.started_at = datetime.realtime(),
    request.input_revision = input_revision,
    request.next_retry_at = null
WITH person, request, input_revision
// Older delivery behavior could leave an expired running request beside a
// replacement queued request. Once one request owns the Person lease, retire
// every other active request for the same type so it cannot generate again.
OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(duplicate:ProfileAnalysisRequest)
WHERE duplicate.analysis_type = request.analysis_type
  AND duplicate.request_id <> request.request_id
  AND duplicate.status IN ['queued', 'running']
WITH person, request, input_revision, collect(duplicate) AS duplicates
FOREACH (stale IN duplicates |
  SET stale.status = 'obsolete',
      stale.completed_at = datetime.realtime()
)
WITH person, input_revision, request.analysis_type AS analysis_type
OPTIONAL MATCH (person)-[:HAS_PROFILE_ANALYSIS]->(history:ProfileAnalysis {
  analysis_type: analysis_type, input_revision: input_revision
})
WITH person, input_revision, analysis_type, count(DISTINCT history) AS history_count
RETURN person.person_id AS person_id,
       input_revision,
       analysis_type = 'sales' AS sales_due,
       CASE WHEN analysis_type = 'sales' THEN history_count + 1 ELSE 1 END
         AS sales_attempt_number,
       analysis_type = 'contact_tracing' AS contact_due,
       CASE WHEN analysis_type = 'contact_tracing' THEN history_count + 1 ELSE 1 END
         AS contact_attempt_number
"""

COMPLETE_PROFILE_ANALYSIS_REQUEST = """
MATCH (person:Person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->
      (request:ProfileAnalysisRequest {request_id: $request_id})
WHERE request.status = 'running'
  AND person.analysis_claim_token = $claim_token
SET request.status = $status,
    request.completed_at = datetime.realtime()
RETURN true AS completed
"""

OBSOLETE_INACTIVE_PROFILE_ANALYSIS_REQUEST = """
MATCH (person:Person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->
      (request:ProfileAnalysisRequest {request_id: $request_id, status: 'queued'})
WHERE person.status <> 'active'
SET request.status = 'obsolete',
    request.completed_at = datetime.realtime()
RETURN true AS completed
"""

PROFILE_ANALYSIS_REQUEST_WAITING = """
MATCH (person:Person)-[:HAS_PROFILE_ANALYSIS_REQUEST]->(request:ProfileAnalysisRequest {
  request_id: $request_id, status: 'queued'
})
RETURN person.status = 'active' AS waiting
"""
