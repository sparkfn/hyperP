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

// Identifier lookups (hot path)
CREATE INDEX idx_identifier_type_norm IF NOT EXISTS
  FOR (id:Identifier) ON (id.identifier_type, id.normalized_value);

CREATE INDEX idx_identifier_type_hash IF NOT EXISTS
  FOR (id:Identifier) ON (id.identifier_type, id.hashed_value);

// Address lookups
CREATE INDEX idx_address_postal IF NOT EXISTS
  FOR (addr:Address) ON (addr.country_code, addr.postal_code);

CREATE INDEX idx_address_composite IF NOT EXISTS
  FOR (addr:Address)
  ON (addr.country_code, addr.postal_code, addr.street_name, addr.street_number);

// Source record
CREATE INDEX idx_source_record_source IF NOT EXISTS
  FOR (sr:SourceRecord) ON (sr.source_record_id);

// Review queue
CREATE INDEX idx_review_case_queue IF NOT EXISTS
  FOR (rc:ReviewCase) ON (rc.queue_state, rc.priority);

// Match decision
CREATE INDEX idx_match_decision_created IF NOT EXISTS
  FOR (md:MatchDecision) ON (md.created_at);

// Person status
CREATE INDEX idx_person_status IF NOT EXISTS
  FOR (p:Person) ON (p.status);

// Full-text search
CREATE FULLTEXT INDEX person_name_search IF NOT EXISTS
  FOR (p:Person) ON EACH [p.preferred_full_name];

CREATE FULLTEXT INDEX address_full_search IF NOT EXISTS
  FOR (addr:Address) ON EACH [addr.normalized_full];
