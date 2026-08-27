// Profile Unifier — Neo4j Constraints and Indexes
// Idempotent — safe to run multiple times.

// Uniqueness constraints
CREATE CONSTRAINT person_id_unique IF NOT EXISTS
  FOR (p:Person) REQUIRE p.person_id IS UNIQUE;

CREATE CONSTRAINT identifier_id_unique IF NOT EXISTS
  FOR (id:Identifier) REQUIRE id.identifier_id IS UNIQUE;

CREATE CONSTRAINT address_id_unique IF NOT EXISTS
  FOR (addr:Address) REQUIRE addr.address_id IS UNIQUE;

CREATE CONSTRAINT source_system_key_unique IF NOT EXISTS
  FOR (ss:SourceSystem) REQUIRE ss.source_key IS UNIQUE;

CREATE CONSTRAINT entity_key_unique IF NOT EXISTS
  FOR (e:Entity) REQUIRE e.entity_key IS UNIQUE;

CREATE CONSTRAINT order_dedup_unique IF NOT EXISTS
  FOR (o:Order) REQUIRE (o.source_system_key, o.source_order_id) IS UNIQUE;

CREATE CONSTRAINT line_item_dedup_unique IF NOT EXISTS
  FOR (li:LineItem) REQUIRE (li.source_system_key, li.source_line_item_id) IS UNIQUE;

CREATE CONSTRAINT product_dedup_unique IF NOT EXISTS
  FOR (p:Product) REQUIRE (p.source_system_key, p.source_product_id) IS UNIQUE;

CREATE CONSTRAINT bankruptcy_case_dedup_unique IF NOT EXISTS
  FOR (bc:BankruptcyCase) REQUIRE (bc.source_system_key, bc.source_case_id) IS UNIQUE;

CREATE CONSTRAINT vehicle_id_unique IF NOT EXISTS
  FOR (v:Vehicle) REQUIRE v.vehicle_id IS UNIQUE;

CREATE CONSTRAINT vehicle_lta_unique IF NOT EXISTS
  FOR (v:Vehicle) REQUIRE v.normalized_lta_tag IS UNIQUE;

CREATE CONSTRAINT source_record_pk_unique IF NOT EXISTS
  FOR (sr:SourceRecord) REQUIRE sr.source_record_pk IS UNIQUE;

CREATE CONSTRAINT match_decision_id_unique IF NOT EXISTS
  FOR (md:MatchDecision) REQUIRE md.match_decision_id IS UNIQUE;

CREATE CONSTRAINT review_case_id_unique IF NOT EXISTS
  FOR (rc:ReviewCase) REQUIRE rc.review_case_id IS UNIQUE;

CREATE CONSTRAINT merge_event_id_unique IF NOT EXISTS
  FOR (me:MergeEvent) REQUIRE me.merge_event_id IS UNIQUE;

CREATE CONSTRAINT ingest_run_id_unique IF NOT EXISTS
  FOR (ir:IngestRun) REQUIRE ir.ingest_run_id IS UNIQUE;

CREATE CONSTRAINT profile_analysis_id_unique IF NOT EXISTS
  FOR (pa:ProfileAnalysis) REQUIRE pa.analysis_id IS UNIQUE;

CREATE CONSTRAINT crm_history_conflict_group_identity_unique IF NOT EXISTS
  FOR (group:CrmHistoryConflictGroup) REQUIRE group.event_identity IS UNIQUE;

CREATE CONSTRAINT crm_history_hash_variant_identity_unique IF NOT EXISTS
  FOR (variant:CrmHistoryHashVariant) REQUIRE (variant.event_identity, variant.canonical_hash) IS UNIQUE;

CREATE CONSTRAINT crm_history_authority_decision_id_unique IF NOT EXISTS
  FOR (decision:CrmHistoryAuthorityDecision) REQUIRE decision.decision_id IS UNIQUE;

CREATE CONSTRAINT crm_history_authority_head_identity_unique IF NOT EXISTS
  FOR (head:CrmHistoryAuthorityHead) REQUIRE head.event_identity IS UNIQUE;

CREATE CONSTRAINT stage_history_unit_identity_unique IF NOT EXISTS
  FOR (unit:StageHistoryUnit) REQUIRE unit.unit_id IS UNIQUE;

CREATE CONSTRAINT stage_history_unit_run_page_unique IF NOT EXISTS
  FOR (unit:StageHistoryUnit)
  REQUIRE (unit.logical_run_id, unit.page_sequence) IS UNIQUE;

CREATE CONSTRAINT stage_history_occurrence_identity_unique IF NOT EXISTS
  FOR (occurrence:StageHistoryOccurrence) REQUIRE occurrence.occurrence_id IS UNIQUE;

CREATE CONSTRAINT stage_history_identity_lock_unique IF NOT EXISTS
  FOR (lock:StageHistoryIdentityLock) REQUIRE lock.event_identity IS UNIQUE;

CREATE CONSTRAINT stage_history_parent_decision_id_unique IF NOT EXISTS
  FOR (decision:CrmHistoryParentAssociationDecision) REQUIRE decision.decision_id IS UNIQUE;

CREATE CONSTRAINT stage_history_retry_identity_unique IF NOT EXISTS
  FOR (retry:StageHistoryRetry)
  REQUIRE (retry.occurrence_id, retry.retry_sequence) IS UNIQUE;

CREATE CONSTRAINT stage_history_review_command_control_id_unique IF NOT EXISTS
  FOR (command:StageHistoryReviewCommand)
  REQUIRE (command.control_instance_id, command.command_id) IS UNIQUE;

CREATE CONSTRAINT bitrix_execution_source_binding_control_unique IF NOT EXISTS
  FOR (binding:BitrixExecutionSourceBinding)
  REQUIRE (binding.source_key, binding.control_instance_id) IS UNIQUE;

CREATE CONSTRAINT stage_history_invalidation_intent_id_unique IF NOT EXISTS
  FOR (intent:CrmHistoryInvalidationIntent) REQUIRE intent.intent_id IS UNIQUE;

CREATE CONSTRAINT stage_history_unit_accounting_identity_unique IF NOT EXISTS
  FOR (accounting:StageHistoryUnitAccounting) REQUIRE accounting.unit_id IS UNIQUE;

// Identifier lookups (hot path)
CREATE INDEX idx_identifier_type_norm IF NOT EXISTS
  FOR (id:Identifier) ON (id.identifier_type, id.normalized_value);

// The legacy index stays in place while identifier-scope migration rolls out.
CREATE INDEX idx_identifier_type_scope_norm IF NOT EXISTS
  FOR (id:Identifier) ON (id.identifier_type, id.identifier_scope, id.normalized_value);

CREATE INDEX idx_identifier_type_hash IF NOT EXISTS
  FOR (id:Identifier) ON (id.identifier_type, id.hashed_value);

// Address lookups
CREATE INDEX idx_address_composite IF NOT EXISTS
  FOR (addr:Address)
  ON (addr.country_code, addr.postal_code, addr.street_name, addr.street_number);

// Source record
CREATE INDEX idx_source_record_source IF NOT EXISTS
  FOR (sr:SourceRecord) ON (sr.source_record_id);

CREATE INDEX idx_source_record_type IF NOT EXISTS
  FOR (sr:SourceRecord) ON (sr.record_type);

CREATE INDEX idx_source_record_link_state IF NOT EXISTS
  FOR (sr:SourceRecord) ON (sr.record_type, sr.link_status);

// Stage-history replay, review, reconciliation, and outbox claim paths.
CREATE INDEX stage_history_unit_run_sequence IF NOT EXISTS
  FOR (unit:StageHistoryUnit) ON (unit.logical_run_id, unit.page_sequence);

CREATE INDEX stage_history_unit_status IF NOT EXISTS
  FOR (unit:StageHistoryUnit) ON (unit.logical_run_id, unit.status);

CREATE INDEX stage_history_occurrence_run_disposition IF NOT EXISTS
  FOR (occurrence:StageHistoryOccurrence)
  ON (occurrence.logical_run_id, occurrence.terminal_disposition);

CREATE INDEX stage_history_occurrence_event_identity IF NOT EXISTS
  FOR (occurrence:StageHistoryOccurrence) ON (occurrence.event_identity);

CREATE INDEX stage_history_parent_decision_event_state IF NOT EXISTS
  FOR (decision:CrmHistoryParentAssociationDecision)
  ON (decision.event_identity, decision.association_state);

CREATE INDEX stage_history_retry_claim_scan IF NOT EXISTS
  FOR (retry:StageHistoryRetry)
  ON (retry.status, retry.next_attempt_at, retry.lease_expires_at);

CREATE INDEX stage_history_review_command_claim_scan IF NOT EXISTS
  FOR (command:StageHistoryReviewCommand)
  ON (command.status, command.lease_expires_at);

CREATE INDEX stage_history_invalidation_claim_scan IF NOT EXISTS
  FOR (intent:CrmHistoryInvalidationIntent)
  ON (intent.status, intent.sequence, intent.lease_expires_at);

CREATE INDEX stage_history_source_record_family IF NOT EXISTS
  FOR (record:SourceRecord) ON (record.record_type, record.history_family);

// Vehicle lookups
CREATE INDEX idx_vehicle_serial IF NOT EXISTS
  FOR (v:Vehicle) ON (v.normalized_serial_number);

// Composite Vehicle lookup for the chat resolve path (queries/vehicle.py
// RESOLVE_EXISTING_VEHICLE_FOR_CHAT). Both identifier columns are scanned
// together — an LTA-tag + serial pair is the unique vehicle identity within
// a source system.
CREATE INDEX idx_vehicle_lta_serial IF NOT EXISTS
  FOR (v:Vehicle) ON (v.normalized_lta_tag, v.normalized_serial_number);

// Review queue
CREATE INDEX idx_review_case_queue IF NOT EXISTS
  FOR (rc:ReviewCase) ON (rc.queue_state, rc.priority);

// Match decision
CREATE INDEX idx_match_decision_created IF NOT EXISTS
  FOR (md:MatchDecision) ON (md.created_at);

// Person status
CREATE INDEX idx_person_status IF NOT EXISTS
  FOR (p:Person) ON (p.status);

CREATE INDEX idx_person_completeness IF NOT EXISTS
  FOR (p:Person) ON (p.profile_completeness_score);

CREATE INDEX idx_person_crm_deal_count IF NOT EXISTS
  FOR (p:Person) ON (p.crm_deal_count);

CREATE INDEX idx_person_high_value IF NOT EXISTS
  FOR (p:Person) ON (p.is_high_value);

CREATE INDEX idx_person_high_risk IF NOT EXISTS
  FOR (p:Person) ON (p.is_high_risk);

CREATE INDEX idx_person_updated_at IF NOT EXISTS
  FOR (p:Person) ON (p.updated_at);

// Deferred KNOWS materialization checks source-record provenance by relationship property.
CREATE INDEX idx_knows_source_record_pk IF NOT EXISTS
  FOR ()-[r:KNOWS]-() ON (r.source_record_pk);

// Profile-analysis invalidation resolves accepted projections by source provenance.
CREATE INDEX idx_identified_by_source_record_pk IF NOT EXISTS
  FOR ()-[r:IDENTIFIED_BY]-() ON (r.source_record_pk);

CREATE INDEX idx_lives_at_source_record_pk IF NOT EXISTS
  FOR ()-[r:LIVES_AT]-() ON (r.source_record_pk);

CREATE INDEX idx_has_fact_source_record_pk IF NOT EXISTS
  FOR ()-[r:HAS_FACT]-() ON (r.source_record_pk);

CREATE INDEX idx_purchased_source_record_pk IF NOT EXISTS
  FOR ()-[r:PURCHASED]-() ON (r.source_record_pk);

CREATE INDEX idx_bought_vehicle_source_record_pk IF NOT EXISTS
  FOR ()-[r:BOUGHT_VEHICLE]-() ON (r.source_record_pk);

CREATE INDEX idx_owns_vehicle_source_record_pk IF NOT EXISTS
  FOR ()-[r:OWNS_VEHICLE]-() ON (r.source_record_pk);

// Profile analysis history
CREATE INDEX idx_profile_analysis_history IF NOT EXISTS
  FOR (pa:ProfileAnalysis)
  ON (pa.person_id, pa.analysis_type, pa.completed_at);

// Sales lookups
CREATE INDEX idx_order_ordered_at IF NOT EXISTS
  FOR (o:Order) ON (o.ordered_at);

CREATE INDEX idx_order_release_date IF NOT EXISTS
  FOR (o:Order) ON (o.release_date);

CREATE INDEX idx_order_status IF NOT EXISTS
  FOR (o:Order) ON (o.status);

CREATE INDEX idx_product_sku IF NOT EXISTS
  FOR (p:Product) ON (p.sku);

CREATE INDEX idx_product_category IF NOT EXISTS
  FOR (p:Product) ON (p.category);

// Bankruptcy lookups
CREATE INDEX idx_bankruptcy_case_number IF NOT EXISTS
  FOR (bc:BankruptcyCase) ON (bc.case_number);

CREATE INDEX idx_bankruptcy_event_date IF NOT EXISTS
  FOR (bc:BankruptcyCase) ON (bc.event_date);

// Full-text search — name, NRIC, email, phone
CREATE FULLTEXT INDEX person_name_search IF NOT EXISTS
  FOR (p:Person) ON EACH [p.preferred_full_name, p.preferred_nric, p.preferred_email, p.preferred_phone];

CREATE FULLTEXT INDEX address_full_search IF NOT EXISTS
  FOR (addr:Address) ON EACH [addr.normalized_full];

// Authoritative CRM stage timeline projection and release gate (#148)
CREATE CONSTRAINT crm_stage_timeline_projection_unique IF NOT EXISTS
  FOR (projection:CrmStageTimelineProjection)
  REQUIRE (projection.mapping_version, projection.event_identity) IS UNIQUE;

CREATE INDEX crm_stage_timeline_parent IF NOT EXISTS
  FOR (projection:CrmStageTimelineProjection)
  ON (projection.parent_source_system, projection.parent_source_record_id, projection.active);

CREATE CONSTRAINT crm_stage_analytical_release_unique IF NOT EXISTS
  FOR (release:CrmStageAnalyticalRelease)
  REQUIRE release.release_key IS UNIQUE;

CREATE CONSTRAINT identity_link_revision_counter_stream_key_unique IF NOT EXISTS
FOR (counter:IdentityLinkRevisionCounter) REQUIRE counter.stream_key IS UNIQUE;
CREATE CONSTRAINT identity_link_head_link_key_unique IF NOT EXISTS
FOR (head:IdentityLinkHead) REQUIRE head.link_key IS UNIQUE;
CREATE CONSTRAINT identity_link_revision_event_id_unique IF NOT EXISTS
FOR (revision:IdentityLinkRevision) REQUIRE revision.event_id IS UNIQUE;
CREATE CONSTRAINT identity_link_revision_cause_key_unique IF NOT EXISTS
FOR (revision:IdentityLinkRevision) REQUIRE revision.cause_key IS UNIQUE;
CREATE CONSTRAINT identity_link_revision_global_revision_unique IF NOT EXISTS
FOR (revision:IdentityLinkRevision) REQUIRE revision.global_revision IS UNIQUE;
CREATE INDEX identity_link_revision_global_revision IF NOT EXISTS
FOR (revision:IdentityLinkRevision) ON (revision.global_revision);
CREATE INDEX identity_link_revision_link_global_revision IF NOT EXISTS
FOR (revision:IdentityLinkRevision) ON (revision.link_key, revision.global_revision);
CREATE INDEX identity_link_head_link_key IF NOT EXISTS
FOR (head:IdentityLinkHead) ON (head.link_key);

// Standalone CRM census control plane (#273); no source/domain facts are created here.
CREATE CONSTRAINT standalone_crm_census_id_unique IF NOT EXISTS
  FOR (node:StandaloneCrmCensus) REQUIRE (node.census_id) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_occurrence_unique IF NOT EXISTS
  FOR (node:StandaloneCrmCensus)
  REQUIRE (node.source_key, node.source_instance_id, node.control_instance_id,
           node.census_kind, node.occurrence_key) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_scope_lock_unique IF NOT EXISTS
  FOR (node:StandaloneCrmCensusScopeLock)
  REQUIRE (node.source_key, node.source_instance_id, node.control_instance_id, node.census_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_attempt_unique IF NOT EXISTS
  FOR (node:StandaloneCrmCensusAttempt) REQUIRE (node.census_id, node.generation) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_unit_unique IF NOT EXISTS
  FOR (node:StandaloneCrmCensusUnit) REQUIRE (node.census_id, node.unit_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_checkpoint_unique IF NOT EXISTS
  FOR (node:StandaloneCrmCensusCheckpoint) REQUIRE (node.census_id, node.unit_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_publication_unique IF NOT EXISTS
  FOR (node:StandaloneCrmChildPublication)
  REQUIRE (node.census_id, node.generation, node.unit_kind, node.sequence) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_call_intent_unique IF NOT EXISTS
  FOR (node:StandaloneCrmHttpCallReservation) REQUIRE (node.intent_id) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_fence_unique IF NOT EXISTS
  FOR (node:StandaloneCrmUnitFence) REQUIRE (node.census_id, node.generation, node.unit_kind) IS UNIQUE;
CREATE INDEX standalone_crm_census_recovery_scan IF NOT EXISTS
  FOR (node:StandaloneCrmCensusAttempt) ON (node.state, node.lease_until);
CREATE INDEX standalone_crm_census_publication_scan IF NOT EXISTS
  FOR (node:StandaloneCrmChildPublication) ON (node.status, node.updated_at);
CREATE INDEX standalone_crm_census_call_scan IF NOT EXISTS
  FOR (node:StandaloneCrmHttpCallReservation) ON (node.census_id, node.generation, node.outcome);
CREATE INDEX standalone_crm_census_fence_scan IF NOT EXISTS
  FOR (node:StandaloneCrmUnitFence) ON (node.state, node.lease_until);
