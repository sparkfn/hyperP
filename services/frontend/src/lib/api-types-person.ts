// Hand-mirrored from services/api/src/types.py and routes/{merge,survivorship}.py.
// Lives outside api-types.ts so the shared module stays untouched.

import type { PersonEntitySummary, SharedIdentifier } from "@/lib/api-types";

export type SourceRecordType = "system" | "conversation";

export interface SourceRecordIdentifierPayload {
  identifier_type?: string;
  normalized_value?: string;
  is_verified?: boolean;
  quality_flag?: string;
}

export interface SourceRecordAddressPayload {
  normalized_full?: string;
  unit_number?: string;
  street_number?: string;
  street_name?: string;
  city?: string;
  postal_code?: string;
  country_code?: string;
  quality_flag?: string;
}

export interface SourceRecordAttributePayload {
  attribute_name?: string;
  attribute_value?: string;
  quality_flag?: string;
}

export interface SourceRecordChatMemberPayload {
  name?: string | null;
  phone?: string | null;
  role?: string | null;
  notes?: string | null;
}

export interface SourceRecordInquiryPayload {
  machine_product?: string | null;
  unit?: string | null;
  lta_tag?: string | null;
  serial_number?: string | null;
  notes?: string | null;
}

export interface SourceRecordNormalizedPayload {
  identifiers?: SourceRecordIdentifierPayload[];
  address?: SourceRecordAddressPayload | null;
  addresses?: SourceRecordAddressPayload[];
  attributes?: SourceRecordAttributePayload[];
  summary?: string;
  customer_sentiment?: string;
  chat_members?: SourceRecordChatMemberPayload[];
  inquiries?: SourceRecordInquiryPayload[];
}

export interface SourceRecordConversationRef {
  platform?: string;
  tenant?: string;
  chat_id?: string;
  deal_id?: string;
  bitrix_chat_id?: string;
  whatsapp_user_id?: string;
  session_id?: string;
}

export interface SourceRecordRawPayload {
  conversation_text?: string;
  messages_text?: string;
  summary?: string;
  customer_sentiment?: string;
  chat_members?: SourceRecordChatMemberPayload[];
  inquiries?: SourceRecordInquiryPayload[];
  tenant?: string;
  category?: string;
  chat_id?: string;
  deal_id?: string;
  bitrix_chat_id?: string;
  whatsapp_user_id?: string;
  session_id?: string;
}

export interface PersonSourceRecord {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  entity_key: string | null;
  entity_display_name: string | null;
  record_type: SourceRecordType;
  extraction_confidence: number | null;
  extraction_method: string | null;
  link_status: string;
  linked_person_id: string | null;
  observed_at: string;
  ingested_at: string;
  conversation_ref: SourceRecordConversationRef | null;
  raw_payload: SourceRecordRawPayload | null;
  normalized_payload: SourceRecordNormalizedPayload | null;
}

export type TimelineTimestampKind = "source" | "fallback";
export type TimelineFactCategory =
  | "identity"
  | "contact"
  | "address"
  | "sale"
  | "relationship"
  | "conversation"
  | "source"
  | "bankruptcy";

export interface PersonTimelineFact {
  fact_id: string;
  category: TimelineFactCategory;
  label: string;
  value: string;
  detail: string | null;
}

export interface PersonTimelineGroup {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  record_type: SourceRecordType;
  extraction_confidence: number | null;
  link_status: string;
  linked_person_id: string | null;
  occurred_at: string;
  timestamp_kind: TimelineTimestampKind;
  ingested_at: string;
  facts: PersonTimelineFact[];
}

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
}

export interface PersonIdentifier {
  identifier_type: string;
  normalized_value: string;
  is_active: boolean;
  is_verified: boolean;
  last_confirmed_at: string | null;
  source_system_key: string | null;
  source_record_pks: string[];
  source_record_ids: string[];
  entities: PersonEntitySummary[];
  source_records: PersonSourceRecord[];
}

export interface PersonSharedIdentifierCandidate {
  person_id: string;
  status: string;
  preferred_full_name: string | null;
  preferred_phone: string | null;
  preferred_email: string | null;
  preferred_dob: string | null;
  profile_completeness_score: number;
  identifier_strength: "strong" | "weak";
  identifiers: SharedIdentifier[];
}

// --- Request bodies ---

export interface GoldenProfileSelectionBody {
  field_name:
    | "preferred_full_name"
    | "preferred_dob"
    | "preferred_phone"
    | "preferred_email"
    | "preferred_address"
    | "preferred_nric";
  source_kind: "source_record_fact" | "identifier" | "address";
  selected_value: string;
  source_record_pk: string | null;
  identifier_type: string | null;
}

export interface ManualMergeRequestBody {
  from_person_id: string;
  to_person_id: string;
  reason: string;
  recompute_golden_profile: boolean;
  golden_profile_selections: GoldenProfileSelectionBody[];
}

export interface UnmergeRequestBody {
  merge_event_id: string;
  reason: string;
}

export interface SurvivorshipOverrideRequestBody {
  attribute_name: string;
  selected_source_record_pk: string;
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
  attribute_name: string;
  selected_source_record_pk: string;
  status: string;
}
