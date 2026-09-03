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

CREATE CONSTRAINT standalone_crm_census_id_unique IF NOT EXISTS
FOR (census:StandaloneCrmCensus) REQUIRE census.census_id IS UNIQUE;
CREATE CONSTRAINT standalone_crm_census_occurrence_unique IF NOT EXISTS
FOR (census:StandaloneCrmCensus) REQUIRE (census.source_key, census.source_instance_id, census.control_instance_id, census.census_kind, census.occurrence_key) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_attempt_identity_unique IF NOT EXISTS
FOR (attempt:StandaloneCrmCensusAttempt) REQUIRE (attempt.census_id, attempt.generation) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_unit_identity_unique IF NOT EXISTS
FOR (unit:StandaloneCrmCensusUnit) REQUIRE (unit.census_id, unit.stream_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_call_intent_unique IF NOT EXISTS
FOR (call:StandaloneCrmHttpCallReservation) REQUIRE call.intent_id IS UNIQUE;
CREATE CONSTRAINT standalone_crm_call_sequence_unique IF NOT EXISTS
FOR (call:StandaloneCrmHttpCallReservation) REQUIRE (call.census_id, call.call_sequence) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_publication_unique IF NOT EXISTS
FOR (publication:StandaloneCrmChildPublication) REQUIRE (publication.census_id, publication.generation, publication.stream_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_checkpoint_unique IF NOT EXISTS
FOR (checkpoint:StandaloneCrmCensusCheckpoint) REQUIRE (checkpoint.census_id, checkpoint.stream_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_fence_unique IF NOT EXISTS
FOR (fence:StandaloneCrmCensusFence) REQUIRE (fence.census_id, fence.generation, fence.stream_kind) IS UNIQUE;
CREATE CONSTRAINT standalone_crm_active_scope_unique IF NOT EXISTS
FOR (scope:StandaloneCrmCensusActiveScope) REQUIRE scope.scope_key IS UNIQUE;
CREATE INDEX standalone_crm_census_active_scope IF NOT EXISTS
FOR (census:StandaloneCrmCensus) ON (census.source_key, census.source_instance_id, census.control_instance_id, census.status);
CREATE INDEX standalone_crm_attempt_lease IF NOT EXISTS
FOR (attempt:StandaloneCrmCensusAttempt) ON (attempt.census_id, attempt.status, attempt.lease_until);
CREATE INDEX standalone_crm_unit_status IF NOT EXISTS
FOR (unit:StandaloneCrmCensusUnit) ON (unit.census_id, unit.generation, unit.state);
CREATE INDEX standalone_crm_publication_status IF NOT EXISTS
FOR (publication:StandaloneCrmChildPublication) ON (publication.census_id, publication.generation, publication.status);

// Standalone CRM Lane A shared contract schema (#301). Keep exactly aligned with
// services/ingestion/src/graph/queries/standalone_crm_lane_a_contracts.py.
CREATE CONSTRAINT crm_company_reference_scope_unique IF NOT EXISTS FOR (n:CrmCompanyReference) REQUIRE (n.source_instance_id, n.company_id) IS UNIQUE;
CREATE CONSTRAINT crm_company_description_observation_id_unique IF NOT EXISTS FOR (n:CrmCompanyDescriptionObservation) REQUIRE n.observation_id IS UNIQUE;
CREATE CONSTRAINT crm_company_description_observation_digest_unique IF NOT EXISTS FOR (n:CrmCompanyDescriptionObservation) REQUIRE n.observation_digest IS UNIQUE;
CREATE CONSTRAINT crm_company_description_head_scope_unique IF NOT EXISTS FOR (n:CrmCompanyDescriptionHead) REQUIRE (n.source_instance_id, n.company_id) IS UNIQUE;
CREATE CONSTRAINT crm_company_membership_snapshot_id_unique IF NOT EXISTS FOR (n:CrmCompanyMembershipSnapshot) REQUIRE n.snapshot_id IS UNIQUE;
CREATE CONSTRAINT crm_company_membership_observation_unique IF NOT EXISTS FOR (n:CrmCompanyMembershipObservation) REQUIRE (n.snapshot_id, n.company_id) IS UNIQUE;
CREATE CONSTRAINT crm_company_membership_head_scope_unique IF NOT EXISTS FOR (n:CrmCompanyMembershipHead) REQUIRE (n.source_instance_id, n.subject_kind, n.subject_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_scope_counter_unique IF NOT EXISTS FOR (n:CrmTenantMappingScopeCounter) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_revision_id_unique IF NOT EXISTS FOR (n:CrmTenantMappingRevision) REQUIRE n.revision_id IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_revision_number_unique IF NOT EXISTS FOR (n:CrmTenantMappingRevision) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id, n.revision_number) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_preparation_request_unique IF NOT EXISTS FOR (n:CrmTenantMappingRevision) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id, n.preparation_request_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_entry_unique IF NOT EXISTS FOR (n:CrmTenantMappingEntry) REQUIRE (n.revision_id, n.company_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_target_unique IF NOT EXISTS FOR (n:CrmTenantMappingTarget) REQUIRE (n.entry_id, n.entity_key, n.relationship_kind) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_mapping_active_head_scope_unique IF NOT EXISTS FOR (n:CrmTenantMappingActiveHead) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_scope_counter_unique IF NOT EXISTS FOR (n:CrmTenantProjectionScopeCounter) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_release_id_unique IF NOT EXISTS FOR (n:CrmTenantProjectionRelease) REQUIRE n.release_id IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_release_number_unique IF NOT EXISTS FOR (n:CrmTenantProjectionRelease) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id, n.release_number) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_request_fingerprint_unique IF NOT EXISTS FOR (n:CrmTenantProjectionRelease) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id, n.request_id, n.release_fingerprint) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_input_unique IF NOT EXISTS FOR (n:CrmTenantProjectionInput) REQUIRE (n.release_id, n.subject_kind, n.subject_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_decision_unique IF NOT EXISTS FOR (n:CrmTenantProjectionDecision) REQUIRE (n.release_id, n.input_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_association_unique IF NOT EXISTS FOR (n:CrmTenantProjectionAssociation) REQUIRE (n.release_id, n.subject_kind, n.subject_id, n.entity_key, n.relationship_kind) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_support_unique IF NOT EXISTS FOR (n:CrmTenantProjectionSupport) REQUIRE (n.association_id, n.membership_observation_id, n.mapping_target_id) IS UNIQUE;
CREATE CONSTRAINT crm_tenant_projection_active_head_scope_unique IF NOT EXISTS FOR (n:CrmTenantProjectionActiveHead) REQUIRE (n.source_key, n.source_instance_id, n.control_instance_id) IS UNIQUE;
CREATE INDEX crm_company_description_observation_order IF NOT EXISTS FOR (n:CrmCompanyDescriptionObservation) ON (n.source_instance_id, n.company_id, n.available_at, n.source_record_version, n.source_record_pk);
CREATE INDEX crm_company_description_head_order IF NOT EXISTS FOR (n:CrmCompanyDescriptionHead) ON (n.source_instance_id, n.company_id, n.available_at, n.source_record_version, n.source_record_pk);
CREATE INDEX crm_company_membership_head_order IF NOT EXISTS FOR (n:CrmCompanyMembershipHead) ON (n.source_instance_id, n.subject_kind, n.subject_id, n.available_at, n.source_record_version, n.source_record_pk);
CREATE INDEX crm_tenant_mapping_revision_state IF NOT EXISTS FOR (n:CrmTenantMappingRevision) ON (n.source_key, n.source_instance_id, n.control_instance_id, n.state, n.revision_number);
CREATE INDEX crm_tenant_projection_release_state IF NOT EXISTS FOR (n:CrmTenantProjectionRelease) ON (n.source_key, n.source_instance_id, n.control_instance_id, n.state, n.release_number);
CREATE INDEX crm_tenant_projection_association_release IF NOT EXISTS FOR (n:CrmTenantProjectionAssociation) ON (n.release_id, n.subject_kind, n.subject_id);

// CRM deal identity repair ledger schema (#300). Keep exactly aligned with
// services/ingestion/src/graph/queries/crm_deal_identity_repair_ledger.py.
CREATE CONSTRAINT crm_deal_repair_boundary_manifest_unique IF NOT EXISTS
FOR (boundary:RepairExecutionBoundary) REQUIRE boundary.manifest_digest IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_boundary_artifact_unique IF NOT EXISTS
FOR (boundary:RepairExecutionBoundary) REQUIRE boundary.artifact_id IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_run_id_unique IF NOT EXISTS
FOR (run:CrmDealRepairRun) REQUIRE run.run_id IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_run_repair_id_unique IF NOT EXISTS
FOR (run:CrmDealRepairRun) REQUIRE run.repair_id IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_run_identity_unique IF NOT EXISTS
FOR (run:CrmDealRepairRun) REQUIRE run.qualification_identity IS UNIQUE;
CREATE INDEX crm_deal_repair_run_status IF NOT EXISTS
FOR (run:CrmDealRepairRun) ON (run.status, run.source_instance_id, run.control_instance_id);
CREATE CONSTRAINT crm_deal_repair_quiescence_unique IF NOT EXISTS
FOR (quiescence:CrmDealRepairQuiescence)
REQUIRE (quiescence.run_id, quiescence.quiescence_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_unit_unique IF NOT EXISTS
FOR (unit:CrmDealRepairUnit) REQUIRE (unit.run_id, unit.unit_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_checkpoint_unique IF NOT EXISTS
FOR (checkpoint:CrmDealRepairCheckpoint) REQUIRE (checkpoint.run_id, checkpoint.checkpoint_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_fence_unique IF NOT EXISTS
FOR (fence:CrmDealRepairFence) REQUIRE (fence.run_id, fence.fence_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_mutation_unique IF NOT EXISTS
FOR (result:CrmDealRepairMutationResult) REQUIRE (result.run_id, result.mutation_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_rollback_unique IF NOT EXISTS
FOR (image:CrmDealRepairRollbackImage) REQUIRE (image.run_id, image.rollback_image_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_rollback_authorization_unique IF NOT EXISTS
FOR (authorization:CrmDealRepairRollbackAuthorization)
REQUIRE (authorization.run_id, authorization.authorization_transition_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_rollback_authorization_slot_unique IF NOT EXISTS
FOR (authorization:CrmDealRepairRollbackAuthorization)
REQUIRE (authorization.run_id, authorization.unit_id, authorization.rollback_image_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_secondary_unique IF NOT EXISTS
FOR (disposition:CrmDealRepairSecondaryDisposition) REQUIRE (disposition.run_id, disposition.disposition_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_verification_unique IF NOT EXISTS
FOR (verification:CrmDealRepairVerification) REQUIRE (verification.run_id, verification.verification_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_outbox_unique IF NOT EXISTS
FOR (outbox:CrmDealRepairOutbox) REQUIRE (outbox.run_id, outbox.event_id) IS UNIQUE;
CREATE INDEX crm_deal_repair_unit_state IF NOT EXISTS
FOR (unit:CrmDealRepairUnit) ON (unit.run_id, unit.state, unit.generation);
CREATE INDEX crm_deal_repair_quiescence_state IF NOT EXISTS
FOR (quiescence:CrmDealRepairQuiescence)
ON (quiescence.run_id, quiescence.state, quiescence.generation, quiescence.sequence);
CREATE INDEX crm_deal_repair_fence_state IF NOT EXISTS
FOR (fence:CrmDealRepairFence) ON (fence.run_id, fence.state, fence.generation);
CREATE INDEX crm_deal_repair_checkpoint_sequence IF NOT EXISTS
FOR (checkpoint:CrmDealRepairCheckpoint)
ON (checkpoint.run_id, checkpoint.unit_id, checkpoint.generation, checkpoint.sequence, checkpoint.attempt);
CREATE INDEX crm_deal_repair_mutation_sequence IF NOT EXISTS
FOR (result:CrmDealRepairMutationResult)
ON (result.run_id, result.unit_id, result.generation, result.sequence, result.attempt);
CREATE INDEX crm_deal_repair_rollback_state IF NOT EXISTS
FOR (image:CrmDealRepairRollbackImage)
ON (image.run_id, image.unit_id, image.generation, image.state);
CREATE INDEX crm_deal_repair_secondary_outcome IF NOT EXISTS
FOR (disposition:CrmDealRepairSecondaryDisposition)
ON (disposition.run_id, disposition.unit_id, disposition.generation, disposition.outcome);
CREATE INDEX crm_deal_repair_verification_outcome IF NOT EXISTS
FOR (verification:CrmDealRepairVerification)
ON (verification.run_id, verification.unit_id, verification.generation, verification.outcome);
CREATE INDEX crm_deal_repair_outbox_state IF NOT EXISTS
FOR (outbox:CrmDealRepairOutbox) ON (outbox.run_id, outbox.state, outbox.sequence);
// CRM deal identity repair control schema (#310). Metadata only; no CRM-domain writes.
CREATE CONSTRAINT crm_deal_repair_control_run_unique IF NOT EXISTS
FOR (control:CrmDealRepairControl) REQUIRE control.run_id IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_publication_reservation_unique IF NOT EXISTS
FOR (reservation:CrmDealRepairPublicationReservation)
REQUIRE (reservation.control_instance_id, reservation.publication_key) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_allocation_completion_unique IF NOT EXISTS
FOR (completion:CrmDealRepairAllocationCompletion) REQUIRE (completion.run_id, completion.completion_id) IS UNIQUE;
CREATE INDEX crm_deal_repair_control_state IF NOT EXISTS
FOR (control:CrmDealRepairControl) ON (control.state, control.control_instance_id, control.revision);

// CRM-deal repair integration schema (#313).
CREATE CONSTRAINT crm_deal_repair_rollback_receipt_unique IF NOT EXISTS
FOR (receipt:CrmDealRepairRollbackReceipt) REQUIRE (receipt.run_id, receipt.receipt_id) IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_acceptance_unique IF NOT EXISTS
FOR (acceptance:CrmDealRepairAcceptance) REQUIRE acceptance.run_id IS UNIQUE;
CREATE CONSTRAINT crm_deal_repair_release_unique IF NOT EXISTS
FOR (release:CrmDealRepairDispatchRelease) REQUIRE release.run_id IS UNIQUE;
