// Hand-maintained mirror of services/api/src/types.py.
// Keep in sync until we wire openapi-typescript codegen against
// docs/profile-unifier-openapi-3.1.yaml.

export type PersonStatus = "active" | "merged" | "suppressed";

export interface ResponseMeta {
  request_id: string;
  next_cursor: string | null;
}

export interface ApiResponse<T> {
  data: T;
  meta: ResponseMeta;
}

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Record<string, string> | null;
}

export interface ApiError {
  error: ApiErrorBody;
  meta: ResponseMeta;
}

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

export interface SourceRecord {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  record_type: "system" | "conversation";
  extraction_confidence: number | null;
  link_status: string;
  linked_person_id: string | null;
  observed_at: string;
  ingested_at: string;
}

export interface SharedIdentifier {
  identifier_type: string;
  normalized_value: string;
}

export interface SharedAddress {
  address_id: string;
  normalized_full: string | null;
}

export interface PersonConnection {
  person_id: string;
  status: string;
  preferred_full_name: string | null;
  hops: number;
  shared_identifiers: SharedIdentifier[];
  shared_addresses: SharedAddress[];
}

export interface GraphNode {
  id: string;
  label: string;
  properties: Record<string, string | number | boolean | null>;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  type: string;
  properties: Record<string, string | number | boolean | null>;
}

export interface PersonGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}
