import type { Role } from "./permissions";

export interface UserResponse {
  email: string;
  google_sub: string;
  role: Role;
  entity_key: string | null;
  display_name: string | null;
}

// Hand-mirrored TS interfaces for ingestion / admin / events / health payloads.
// Kept separate from api-types.ts so the core domain types stay untouched.
// Mirrors services/api/src/types.py and the response models defined inline in
// services/api/src/routes/{ingest,admin,events,health}.py.

export type TrustTier = "tier_1" | "tier_2" | "tier_3" | "tier_4";

export const TRUST_TIERS: readonly TrustTier[] = [
  "tier_1",
  "tier_2",
  "tier_3",
  "tier_4",
] as const;

export function isTrustTier(value: string): value is TrustTier {
  return (TRUST_TIERS as readonly string[]).includes(value);
}

export type IngestRecordType = "system" | "conversation";

export interface IngestIdentifier {
  type: string;
  value: string;
  is_verified: boolean;
}

export type IngestScalar = string | number | boolean | null;

export interface IngestRecord {
  source_record_id: string;
  source_record_version: string | null;
  record_type: IngestRecordType;
  extraction_confidence: number | null;
  extraction_method: string | null;
  conversation_ref: Record<string, IngestScalar> | null;
  observed_at: string;
  record_hash: string;
  identifiers: IngestIdentifier[];
  attributes: Record<string, IngestScalar>;
  raw_payload: Record<string, IngestScalar>;
}

export interface IngestRecordsRequest {
  ingest_type: string;
  ingest_run_id: string | null;
  records: IngestRecord[];
}

export interface IngestRecordResult {
  source_record_id: string;
  status: string;
}

export interface IngestRecordsResponse {
  accepted_count: number;
  rejected_count: number;
  ingest_run_id: string;
  results: IngestRecordResult[];
}

export interface IngestRunCreateRequest {
  run_type: string;
  metadata: Record<string, string>;
}

export interface IngestRunUpdateRequest {
  status: string;
  finished_at: string | null;
  metadata: Record<string, string> | null;
}

export interface IngestRunResponse {
  ingest_run_id: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface IngestRunDetailResponse {
  ingest_run_id: string;
  run_type: string;
  status: string;
  record_count: number;
  rejected_count: number;
  started_at: string | null;
  finished_at: string | null;
  source_key: string | null;
}

export interface SourceSystemInfo {
  source_system_id: string | null;
  source_key: string;
  display_name: string | null;
  system_type: string | null;
  is_active: boolean;
  field_trust: Record<string, string>;
  entity_key: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface FieldTrustResponse {
  source_key: string;
  display_name: string | null;
  field_trust: Record<string, string>;
}

export interface FieldTrustUpdateRequest {
  updates: Record<string, TrustTier>;
}

export interface DownstreamEvent {
  event_id: string;
  event_type: string;
  affected_person_ids: string[];
  metadata: Record<string, string>;
  created_at: string;
}

// --- Review queue ---
// Mirrors services/api/src/types.py ReviewCaseSummary / ReviewCaseDetail and
// routes/review.py request + response models.

export type ReviewActionType =
  | "merge"
  | "reject"
  | "defer"
  | "escalate"
  | "manual_no_match";

export const REVIEW_ACTION_TYPES: readonly ReviewActionType[] = [
  "merge",
  "reject",
  "defer",
  "escalate",
  "manual_no_match",
] as const;

export type QueueState =
  | "open"
  | "assigned"
  | "deferred"
  | "resolved"
  | "cancelled";

export const QUEUE_STATES: readonly QueueState[] = [
  "open",
  "assigned",
  "deferred",
  "resolved",
  "cancelled",
] as const;

export function isQueueState(value: string): value is QueueState {
  return (QUEUE_STATES as readonly string[]).includes(value);
}

export interface MatchDecisionSummary {
  match_decision_id: string;
  engine_type: string;
  decision: string;
  confidence: number;
}

export interface ReviewCaseSummary {
  review_case_id: string;
  queue_state: string;
  priority: number;
  assigned_to: string | null;
  follow_up_at: string | null;
  sla_due_at: string | null;
  match_decision: MatchDecisionSummary;
}

export interface ReviewMatchDecision {
  match_decision_id: string;
  engine_type: string;
  engine_version: string;
  policy_version: string;
  decision: string;
  confidence: number;
  reasons: string[];
  blocking_conflicts: string[];
  created_at: string;
  left_person_id: string | null;
  right_person_id: string | null;
}

export interface ReviewAddressSummary {
  address_id: string;
  unit_number: string | null;
  street_number: string | null;
  street_name: string | null;
  city: string | null;
  postal_code: string | null;
  country_code: string | null;
  normalized_full: string | null;
}

export type ComparisonEntityKind = "person" | "source_record";

export interface PersonComparisonEntity {
  entity_kind: ComparisonEntityKind;
  person_id: string | null;
  source_record_pk: string | null;
  source_record_id: string | null;
  status: string | null;
  preferred_full_name: string | null;
  preferred_phone: string | null;
  preferred_email: string | null;
  preferred_dob: string | null;
  preferred_address: ReviewAddressSummary | null;
}

export interface ReviewCaseActionEntry {
  [key: string]: string | null;
}

export interface ReviewCaseDetail {
  review_case_id: string;
  queue_state: string;
  priority: number;
  assigned_to: string | null;
  follow_up_at: string | null;
  sla_due_at: string | null;
  resolution: string | null;
  resolved_at: string | null;
  actions: ReviewCaseActionEntry[];
  match_decision: ReviewMatchDecision;
  comparison_left: PersonComparisonEntity | null;
  comparison_right: PersonComparisonEntity | null;
  created_at: string;
  updated_at: string;
}

export interface AssignReviewRequestBody {
  assigned_to: string;
}

export interface ReviewActionMetadataBody {
  create_manual_lock?: boolean;
  follow_up_at?: string | null;
  escalation_reason?: string | null;
}

export interface ReviewActionRequestBody {
  action_type: ReviewActionType;
  notes?: string | null;
  metadata?: ReviewActionMetadataBody;
}

export interface ReviewAssignResponse {
  review_case_id: string;
  queue_state: string;
  assigned_to: string;
}

export interface ReviewActionResponse {
  review_case_id: string;
  queue_state: string;
  resolution: string | null;
}

export interface HealthResponse {
  status: string;
  neo4j: string;
  timestamp: string;
  error: string | null;
}
