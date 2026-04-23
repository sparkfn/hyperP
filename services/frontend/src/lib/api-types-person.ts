// Hand-mirrored from services/api/src/types.py and routes/{merge,survivorship}.py.
// Lives outside api-types.ts so the shared module stays untouched.

export type SourceRecordType = "system" | "conversation";

export interface PersonSourceRecord {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  record_type: SourceRecordType;
  extraction_confidence: number | null;
  link_status: string;
  linked_person_id: string | null;
  observed_at: string;
  ingested_at: string;
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
}

// --- Request bodies ---

export interface ManualMergeRequestBody {
  from_person_id: string;
  to_person_id: string;
  reason: string;
  recompute_golden_profile: boolean;
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
