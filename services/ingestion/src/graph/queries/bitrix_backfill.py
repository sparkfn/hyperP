"""Cypher for Bitrix corrective-generation control and coverage export."""

from __future__ import annotations

EXPORT_FROZEN_OWNER_COVERAGE = """
MATCH (generation:BitrixBackfillGeneration {generation_id: $generation_id})
WHERE generation.status IN ['frozen', 'qualified', 'accepted']
MATCH (coverage:BitrixBackfillCoverage {
  generation_id: $generation_id,
  stream_key: 'crm_deals'
})
WHERE coverage.scope_state = 'in_scope'
  AND coverage.terminal = true
  AND coverage.deal_id IS NOT NULL
  AND coverage.category_id IS NOT NULL
RETURN coverage.deal_id AS deal_id,
       coverage.category_id AS category_id,
       coverage.stage_id AS stage_id,
       coverage.source_observation_hash AS source_observation_hash,
       generation.source_contract_uuid AS source_contract_uuid,
       generation.configuration_digest AS configuration_digest,
       generation.image_digest AS image_digest,
       generation.boundary_digest AS boundary_digest,
       generation.owner_count AS expected_owner_count,
       generation.owner_set_digest AS expected_owner_set_digest
ORDER BY toInteger(coverage.deal_id), coverage.deal_id
"""
