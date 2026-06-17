// Hand-mirrored from services/api/src/types.py and routes/{merge,survivorship}.py.
// Lives outside api-types.ts so the shared module stays untouched.

import type { PersonEntitySummary, SourceRecordType } from "./api-types";

export type { SourceRecordType };

export interface PersonBankruptcyCase {
  bankruptcy_case_id: string;
  source_system_key: string;
  source_case_id: string;
  case_number: string | null;
  document_type: string | null;
  document_date: string | null;
  event_type: string | null;
  event_date: string | null;
  trustee_name: string | null;
  trustee_firm: string | null;
  source_url: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SourceRecordIdentifierPayload {
  identifier_type?: string;
  normalized_value?: string;
  is_verified?: boolean;
  quality_flag?: string;
}

export interface SourceRecordAddressPayload {
  normalized_full?: string | null;
  unit_number?: string | null;
  street_number?: string | null;
  street_name?: string | null;
  city?: string | null;
  postal_code?: string | null;
  country_code?: string | null;
  quality_flag?: string | null;
}

export interface SourceRecordAttributePayload {
  attribute_name?: string;
  attribute_value?: string;
  quality_flag?: string;
}

export interface SourceRecordNormalizedPayload {
  identifiers?: SourceRecordIdentifierPayload[];
  address?: SourceRecordAddressPayload | null;
  attributes?: SourceRecordAttributePayload[];
  summary?: string;
}

export interface ChatMessage {
  timestamp: string;
  timestamp_display: string;
  speaker: string;
  phone: string | null;
  role: string | null;
  text: string;
}

export interface PersonSourceRecord {
  source_record_pk: string;
  source_system: string;
  entity_key: string | null;
  entity_display_name: string | null;
  source_record_id: string;
  source_record_version: string | null;
  record_type: SourceRecordType;
  extraction_method: string | null;
  extraction_confidence: number | null;
  link_status: string;
  linked_person_id: string | null;
  observed_at: string;
  ingested_at: string;
  normalized_payload: SourceRecordNormalizedPayload | null;
  // raw_payload/conversation_ref are free-form source JSON with no fixed schema;
  // the UI only pretty-prints them via JSON.stringify, never indexes them by key.
  raw_payload: Record<string, unknown> | null;
  conversation_ref: Record<string, unknown> | null;
  observed_at_display: string;
  ingested_at_display: string;
  extraction_confidence_display: string | null;
  chat_transcript: ChatMessage[] | null;
}

export interface PersonAuditEvent {
  merge_event_id: string;
  event_type: string;
  actor_type: string;
  actor_id: string;
  reason: string | null;
  metadata: Record<string, string>;
  created_at: string;
  absorbed_person_id: string | null;
  survivor_person_id: string | null;
  triggered_by_decision_id: string | null;
}

export type MatchEngineType = "deterministic" | "heuristic" | "llm" | "manual";
export type MatchOutcome = "merge" | "review" | "no_match";

export interface PersonMatchDecision {
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
  review_case_id: string | null;
  review_case_queue_state: string | null;
  review_case_assigned_to: string | null;
}

export interface PersonIdentifier {
  identifier_type: string;
  normalized_value: string;
  is_active: boolean;
  is_verified: boolean;
  last_confirmed_at: string | null;
  source_system_key: string | null;
  source_record_ids: string[] | null;
  entities: PersonEntitySummary[];
  source_records: PersonSourceRecord[];
}

// --- Request bodies ---

export type GoldenProfileSelectionSourceKind = "source_record_fact" | "identifier" | "address" | "literal";

export interface GoldenProfileSelectionRequestBody {
  field_name: "preferred_full_name" | "preferred_dob" | "preferred_phone" | "preferred_email" | "preferred_address" | "preferred_nric";
  source_kind: GoldenProfileSelectionSourceKind;
  selected_value: string;
  source_record_pk?: string | null;
  identifier_type?: string | null;
}

export interface ManualMergeRequestBody {
  from_person_id: string;
  to_person_id: string;
  reason: string;
  recompute_golden_profile: boolean;
  golden_profile_selections?: GoldenProfileSelectionRequestBody[];
}

export interface UnmergeRequestBody {
  merge_event_id: string;
  reason: string;
}

export type GoldenFieldName =
  | "preferred_full_name"
  | "preferred_dob"
  | "preferred_phone"
  | "preferred_email"
  | "preferred_nric"
  | "preferred_address";

export type GoldenSourceKind = "source_record_fact" | "identifier" | "address";

export interface FieldOption {
  source_record_pk: string;
  source_kind: GoldenSourceKind;
  identifier_type: string | null;
  value: string;
  value_display: string;
  source_system: string;
  entity_display_name: string | null;
  observed_at_display: string | null;
  is_current: boolean;
}

export interface EditableFieldOptions {
  field_name: GoldenFieldName;
  label: string;
  source_kind: GoldenSourceKind;
  current_value_display: string | null;
  is_overridden: boolean;
  options: FieldOption[];
}

export interface PersonFieldOptions {
  person_id: string;
  fields: EditableFieldOptions[];
}

export interface SurvivorshipOverrideRequestBody {
  field_name: GoldenFieldName;
  source_record_pk?: string;
  custom_value?: string;
  reason: string;
}

// --- Response bodies ---

export interface ManualMergeResponseBody {
  merge_event_id: string;
  from_person_id: string;
  to_person_id: string;
  status: string;
}

export interface UnmergeResponseBody {
  merge_event_id: string;
  absorbed_person_id: string;
  survivor_person_id: string;
  status: string;
}

export interface SurvivorshipOverrideResponseBody {
  person_id: string;
  field_name: string;
  source_record_pk: string;
  status: string;
}

// --- Possible matches (shared-identifiers) ---

export interface PersonSharedIdentifierCandidate {
  person_id: string;
  status: string;
  preferred_full_name: string | null;
  preferred_phone: string | null;
  preferred_email: string | null;
  preferred_dob: string | null;
  profile_completeness_score: number;
  /** "strong" = high-confidence identifiers (NRIC, phone); "weak" = lower-confidence */
  identifier_strength: "strong" | "weak";
  identifiers: Array<{ identifier_type: string; normalized_value: string }>;
}

export interface SharedIdentifierGroup {
  identifier_type: string;
  normalized_value: string;
  candidate_source_records: PersonSourceRecord[];
  current_person_source_records: PersonSourceRecord[];
}

export interface PossibleMatchDetail {
  candidate_person_id: string;
  candidate_name: string | null;
  shared_identifier_groups: SharedIdentifierGroup[];
}

export interface SourceRecordEntityFacet {
  source_system: string;
  entity_key: string | null;
  entity_display_name: string | null;
  count: number;
}
