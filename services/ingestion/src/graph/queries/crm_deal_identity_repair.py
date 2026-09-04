"""Read-only Cypher inventory for historical CRM-deal identity repair."""

from __future__ import annotations

INVENTORY_ACTIVE_CRM_DEALS = """
MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[:FROM_SOURCE]->
      (:SourceSystem {source_key: $source_system})
WITH deal
ORDER BY deal.source_record_pk
SKIP $skip
LIMIT $limit
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[link:LINKED_TO]->(owner:Person)
    WITH link, owner
    WHERE link IS NOT NULL
    RETURN collect({
        person_id: owner.person_id,
        is_active: coalesce(link.is_active, true),
        relationship_type: type(link),
        relationship_properties: properties(link)
    }) AS linked_people
}
CALL {
    WITH deal
    MATCH (version:SourceRecord {source_record_id: deal.source_record_id, record_type: 'crm_deal'})
          -[:FROM_SOURCE]->(:SourceSystem {source_key: $source_system})
    RETURN collect({
        source_record_pk: version.source_record_pk,
        source_record_version: version.source_record_version,
        lifecycle_status: version.lifecycle_status,
        is_latest: version.is_latest,
        raw_payload: version.raw_payload,
        normalized_payload: version.normalized_payload
    }) AS logical_versions
}
CALL {
    WITH deal
    OPTIONAL MATCH (descendant:SourceRecord)-[:CHILD_OF*1..2]->(deal)
    OPTIONAL MATCH (descendant)-[descendant_link:LINKED_TO]->(descendant_owner:Person)
    WITH descendant, descendant_link, descendant_owner
    WHERE descendant IS NOT NULL
    RETURN collect({
        record_type: descendant.record_type,
        source_record_pk: descendant.source_record_pk,
        source_record_id: descendant.source_record_id,
        lifecycle_status: descendant.lifecycle_status,
        relationship_type: type(descendant_link),
        relationship_is_active: coalesce(descendant_link.is_active, true),
        owner_person_id: descendant_owner.person_id
    }) AS descendants
}
CALL {
    WITH deal
    OPTIONAL MATCH (decision:MatchDecision)-[:ABOUT_LEFT|ABOUT_RIGHT]->(deal)
    OPTIONAL MATCH (review:ReviewCase)-[:FOR_DECISION]->(decision)
    WITH decision, review
    WHERE decision IS NOT NULL OR review IS NOT NULL
    RETURN collect(DISTINCT {
        evidence_type: 'record_to_person',
        match_decision_id: decision.match_decision_id,
        decision: decision.decision,
        policy_version: decision.policy_version,
        engine_type: decision.engine_type,
        review_case_id: review.review_case_id,
        review_resolution: review.resolution,
        review_queue_state: review.queue_state
    }) AS record_decisions_and_reviews
}
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[:LINKED_TO]->(owner:Person)
    OPTIONAL MATCH (pair_decision:MatchDecision)-[owner_about:ABOUT_LEFT|ABOUT_RIGHT]->(owner)
    OPTIONAL MATCH (pair_decision)-[counterpart_about:ABOUT_LEFT|ABOUT_RIGHT]->(counterpart:Person)
    OPTIONAL MATCH (pair_review:ReviewCase)-[:FOR_DECISION]->(pair_decision)
    WITH owner, pair_decision, owner_about, counterpart, counterpart_about, pair_review
    WHERE pair_decision IS NOT NULL
      AND pair_decision.engine_type = 'pair_audit'
      AND owner_about.entity_type = 'person'
      AND counterpart_about.entity_type = 'person'
      AND counterpart <> owner
    RETURN collect(DISTINCT {
        evidence_type: 'pair_audit',
        owner_person_id: owner.person_id,
        counterpart_person_id: counterpart.person_id,
        owner_about_relationship_type: type(owner_about),
        counterpart_about_relationship_type: type(counterpart_about),
        match_decision_id: pair_decision.match_decision_id,
        decision: pair_decision.decision,
        policy_version: pair_decision.policy_version,
        engine_type: pair_decision.engine_type,
        review_case_id: pair_review.review_case_id,
        review_resolution: pair_review.resolution,
        review_queue_state: pair_review.queue_state
    }) AS pair_decisions_and_reviews
}
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[:LINKED_TO]->(owner:Person)
    WITH owner
    WHERE owner IS NOT NULL
    RETURN collect({
        evidence_type: 'owner_profile',
        owner_person_id: owner.person_id,
        survivorship_overrides: owner.survivorship_overrides,
        crm_deal_count: owner.crm_deal_count,
        golden_profile_version: owner.golden_profile_version
    }) AS owner_profiles
}
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[:LINKED_TO]->(owner:Person)
    OPTIONAL MATCH (owner)-[lock:NO_MATCH_LOCK]-(other:Person)
    WITH owner, lock, other
    WHERE lock IS NOT NULL
    RETURN collect({
        evidence_type: 'no_match_lock',
        owner_person_id: owner.person_id,
        no_match_lock_id: lock.lock_id,
        lock_other_person_id: other.person_id
    }) AS owner_locks
}
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[:LINKED_TO]->(owner:Person)
    OPTIONAL MATCH (owner)-[outgoing_rel:MERGED_INTO]->(survivor:Person)
    WITH owner, outgoing_rel, survivor
    WHERE outgoing_rel IS NOT NULL
    RETURN collect(DISTINCT {
        evidence_type: 'merge_lineage',
        direction: 'outgoing',
        owner_person_id: owner.person_id,
        merge_event_id: outgoing_rel.merge_event_id,
        absorbed_person_id: owner.person_id,
        survivor_person_id: survivor.person_id
    }) AS outgoing_owner_merges
}
CALL {
    WITH deal
    OPTIONAL MATCH (deal)-[:LINKED_TO]->(owner:Person)
    OPTIONAL MATCH (absorbed:Person)-[incoming_rel:MERGED_INTO]->(owner)
    WITH owner, incoming_rel, absorbed
    WHERE incoming_rel IS NOT NULL
    RETURN collect(DISTINCT {
        evidence_type: 'merge_lineage',
        direction: 'incoming',
        owner_person_id: owner.person_id,
        merge_event_id: incoming_rel.merge_event_id,
        absorbed_person_id: absorbed.person_id,
        survivor_person_id: owner.person_id
    }) AS incoming_owner_merges
}
RETURN deal.source_record_pk AS source_record_pk,
       deal.source_record_id AS source_record_id,
       deal.source_record_version AS source_record_version,
       deal.lifecycle_status AS lifecycle_status,
       deal.is_latest AS is_latest,
       deal.record_hash AS record_hash,
       toString(deal.observed_at) AS observed_at,
       deal.raw_payload AS raw_payload,
       deal.normalized_payload AS normalized_payload,
       linked_people, logical_versions, descendants,
       record_decisions_and_reviews + pair_decisions_and_reviews AS decisions_and_reviews,
       owner_profiles + owner_locks + outgoing_owner_merges + incoming_owner_merges AS owner_impacts
ORDER BY deal.source_record_id, deal.source_record_pk
"""

_CRM_DEAL_PREFIX = """
MATCH (deal:SourceRecord {record_type: 'crm_deal'})-[:FROM_SOURCE]->
      (:SourceSystem {source_key: $source_system})
"""

_CRM_DEAL_PROJECTION_RETURN = """
RETURN deal.source_record_pk AS source_record_pk,
       {
         relationship_type: type(projection),
         is_active: coalesce(projection.is_active, true),
         relationship_properties: properties(projection),
         owner_person_id: __OWNER_PERSON__,
         identifier_type: target.identifier_type,
         identifier_value: target.normalized_value,
         address_id: target.address_id,
         target_source_record_pk: target.source_record_pk,
         source_record_pk: projection.source_record_pk
       } AS projection
"""

_CRM_DEAL_PROJECTION_RELATIONSHIP_TYPES = (
    "BOUGHT_VEHICLE",
    "HAS_BANKRUPTCY_CASE",
    "HAS_FACT",
    "IDENTIFIED_BY",
    "INVOLVES_VEHICLE",
    "KNOWS",
    "LIVES_AT",
    "MENTIONS_VEHICLE",
    "MOVED_RELATIONSHIP",
    "OWNS_VEHICLE",
    "PURCHASED",
)


def _crm_deal_projection_branch(
    relationship_type: str,
    *,
    deal_owned_address: bool = False,
) -> str:
    """Build one indexed branch of the CRM-deal projection inventory query."""
    if deal_owned_address:
        pattern = "MATCH (deal)-[projection:DESCRIBES_ADDRESS]->(target)"
        condition = "WHERE projection.source_record_pk IS NULL"
    else:
        pattern = f"MATCH (start)-[projection:{relationship_type}]->(target)"
        condition = "WHERE projection.source_record_pk = deal.source_record_pk"
    owner_person = "null" if deal_owned_address else "start.person_id"
    return "\n".join(
        (
            _CRM_DEAL_PREFIX.strip(),
            pattern,
            condition,
            _CRM_DEAL_PROJECTION_RETURN.replace("__OWNER_PERSON__", owner_person).strip(),
        )
    )


def _crm_deal_projection_query() -> str:
    """Use only explicit relationship types so each branch can use its index."""
    branches = [
        _crm_deal_projection_branch(relationship_type)
        for relationship_type in _CRM_DEAL_PROJECTION_RELATIONSHIP_TYPES
    ]
    branches.extend(
        (
            _crm_deal_projection_branch("DESCRIBES_ADDRESS"),
            _crm_deal_projection_branch("DESCRIBES_ADDRESS", deal_owned_address=True),
        )
    )
    return "\nUNION ALL\n".join(branches)


INVENTORY_CRM_DEAL_PROJECTIONS = _crm_deal_projection_query()

INVENTORY_STALE_RUN_CONTROL_PLANE = """
OPTIONAL MATCH (run:IngestRun {ingest_run_id: $stale_run_id})
OPTIONAL MATCH (run)-[:FROM_SOURCE]->(source:SourceSystem)
OPTIONAL MATCH (logical:IngestionLogicalRun)-[:HAS_ATTEMPT]->(run)
OPTIONAL MATCH (checkpoint:IngestionCheckpoint {logical_run_id: logical.logical_run_id})
RETURN $stale_run_id AS stale_run_id,
       'unknown' AS stale_run_state,
       run.status AS run_status,
       source.source_key AS associated_source_system,
       count(DISTINCT logical) AS logical_run_association_count,
       count(DISTINCT checkpoint) AS checkpoint_association_count
"""
