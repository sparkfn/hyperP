// --- Enums ---

export const PersonStatus = {
  ACTIVE: 'active',
  MERGED: 'merged',
  SUPPRESSED: 'suppressed',
} as const;
export type PersonStatus = (typeof PersonStatus)[keyof typeof PersonStatus];

export const QualityFlag = {
  VALID: 'valid',
  INVALID_FORMAT: 'invalid_format',
  PLACEHOLDER_VALUE: 'placeholder_value',
  SHARED_SUSPECTED: 'shared_suspected',
  STALE: 'stale',
  SOURCE_UNTRUSTED: 'source_untrusted',
  PARTIAL_PARSE: 'partial_parse',
} as const;
export type QualityFlag = (typeof QualityFlag)[keyof typeof QualityFlag];

export const IdentifierType = {
  PHONE: 'phone',
  EMAIL: 'email',
  GOVERNMENT_ID_HASH: 'government_id_hash',
  EXTERNAL_CUSTOMER_ID: 'external_customer_id',
  MEMBERSHIP_ID: 'membership_id',
  CRM_CONTACT_ID: 'crm_contact_id',
  LOYALTY_ID: 'loyalty_id',
  CUSTOM: 'custom',
} as const;
export type IdentifierType = (typeof IdentifierType)[keyof typeof IdentifierType];

export const EngineType = {
  DETERMINISTIC: 'deterministic',
  HEURISTIC: 'heuristic',
  LLM: 'llm',
  MANUAL: 'manual',
} as const;
export type EngineType = (typeof EngineType)[keyof typeof EngineType];

export const MatchDecisionOutcome = {
  MERGE: 'merge',
  REVIEW: 'review',
  NO_MATCH: 'no_match',
} as const;
export type MatchDecisionOutcome = (typeof MatchDecisionOutcome)[keyof typeof MatchDecisionOutcome];

export const QueueState = {
  OPEN: 'open',
  ASSIGNED: 'assigned',
  DEFERRED: 'deferred',
  RESOLVED: 'resolved',
  CANCELLED: 'cancelled',
} as const;
export type QueueState = (typeof QueueState)[keyof typeof QueueState];

export const LinkStatus = {
  LINKED: 'linked',
  PENDING_REVIEW: 'pending_review',
  REJECTED: 'rejected',
  SUPPRESSED: 'suppressed',
} as const;
export type LinkStatus = (typeof LinkStatus)[keyof typeof LinkStatus];

/** Action types submittable via the review actions API endpoint. */
export const ApiReviewActionType = {
  MERGE: 'merge',
  REJECT: 'reject',
  DEFER: 'defer',
  ESCALATE: 'escalate',
  MANUAL_NO_MATCH: 'manual_no_match',
} as const;
export type ApiReviewActionType = (typeof ApiReviewActionType)[keyof typeof ApiReviewActionType];

/** All review action types including system-recorded ones. */
export const ReviewActionType = {
  ...ApiReviewActionType,
  ASSIGN: 'assign',
  UNASSIGN: 'unassign',
  CANCEL: 'cancel',
  REOPEN: 'reopen',
} as const;
export type ReviewActionType = (typeof ReviewActionType)[keyof typeof ReviewActionType];

export const MergeEventType = {
  PERSON_CREATED: 'person_created',
  AUTO_MERGE: 'auto_merge',
  MANUAL_MERGE: 'manual_merge',
  REVIEW_REJECT: 'review_reject',
  MANUAL_NO_MATCH: 'manual_no_match',
  UNMERGE: 'unmerge',
  PERSON_SPLIT: 'person_split',
  SURVIVORSHIP_OVERRIDE: 'survivorship_override',
} as const;
export type MergeEventType = (typeof MergeEventType)[keyof typeof MergeEventType];

export const LockType = {
  MANUAL_NO_MATCH: 'manual_no_match',
  MANUAL_MERGE_HINT: 'manual_merge_hint',
  PERSON_SUPPRESSION: 'person_suppression',
} as const;
export type LockType = (typeof LockType)[keyof typeof LockType];

export const EventType = {
  PERSON_CREATED: 'person_created',
  PERSON_MERGED: 'person_merged',
  PERSON_UNMERGED: 'person_unmerged',
  GOLDEN_PROFILE_UPDATED: 'golden_profile_updated',
  REVIEW_CASE_RESOLVED: 'review_case_resolved',
  SHARED_IDENTIFIER_DETECTED: 'shared_identifier_detected',
  RELATIONSHIP_CREATED: 'relationship_created',
} as const;
export type EventType = (typeof EventType)[keyof typeof EventType];

// --- Interfaces ---

export interface AddressSummary {
  address_id: string;
  unit_number: string | null;
  street_number: string | null;
  street_name: string | null;
  city: string | null;
  postal_code: string | null;
  country_code: string | null;
  normalized_full: string | null;
}

export interface Person {
  person_id: string;
  status: PersonStatus;
  is_high_value: boolean;
  is_high_risk: boolean;
  preferred_full_name: string | null;
  preferred_phone: string | null;
  preferred_email: string | null;
  preferred_dob: string | null;
  preferred_address: AddressSummary | null;
  profile_completeness_score: number;
  golden_profile_computed_at: string | null;
  golden_profile_version: string | null;
  source_record_count: number;
  created_at: string;
  updated_at: string;
}

export interface Identifier {
  identifier_id: string;
  identifier_type: IdentifierType;
  normalized_value: string | null;
  hashed_value: string | null;
  is_verified: boolean;
  is_active: boolean;
  quality_flag: QualityFlag;
  source_system_key: string;
  source_record_pk: string;
  first_seen_at: string;
  last_seen_at: string;
  last_confirmed_at: string | null;
}

export interface SourceRecord {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  link_status: LinkStatus;
  linked_person_id: string | null;
  observed_at: string;
  ingested_at: string;
}

export interface MatchDecision {
  match_decision_id: string;
  engine_type: EngineType;
  engine_version: string;
  policy_version: string;
  decision: MatchDecisionOutcome;
  confidence: number;
  reasons: string[];
  blocking_conflicts: string[];
  created_at: string;
}

export interface ReviewAction {
  action_type: ReviewActionType;
  actor_type: string;
  actor_id: string;
  notes: string | null;
  created_at: string;
}

export interface ReviewCase {
  review_case_id: string;
  queue_state: QueueState;
  priority: number;
  assigned_to: string | null;
  follow_up_at: string | null;
  sla_due_at: string | null;
  resolution: string | null;
  resolved_at: string | null;
  match_decision: MatchDecision | null;
  actions: ReviewAction[];
  created_at: string;
  updated_at: string;
}

export interface SharedIdentifier {
  identifier_type: string;
  normalized_value: string;
}

export interface SharedAddress {
  address_id: string;
  normalized_full: string;
}

export interface PersonConnection {
  person_id: string;
  status: PersonStatus;
  preferred_full_name: string | null;
  hops: number;
  shared_identifiers: SharedIdentifier[];
  shared_addresses: SharedAddress[];
}

export interface ApiResponse<T> {
  data: T;
  meta: {
    request_id: string;
    next_cursor?: string | null;
  };
}

export interface ApiError {
  error: {
    code: string;
    message: string;
    details?: Record<string, unknown>;
  };
  meta: {
    request_id: string;
  };
}
