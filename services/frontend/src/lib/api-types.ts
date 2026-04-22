// Hand-maintained mirror of services/api/src/types.py.
// Keep in sync until we wire openapi-typescript codegen against
// docs/profile-unifier-openapi-3.1.yaml.

export type PersonStatus = "active" | "merged" | "suppressed";

export interface ResponseMeta {
  request_id: string;
  next_cursor: string | null;
  total_count?: number | null;
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
  preferred_nric: string | null;
  profile_completeness_score: number;
  golden_profile_computed_at: string | null;
  golden_profile_version: string | null;
  source_record_count: number;
  connection_count: number;
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

export interface KnowsRelationship {
  relationship_label: string | null;
  relationship_category: string;
}

export interface PersonConnection {
  person_id: string;
  status: string;
  preferred_full_name: string | null;
  hops: number;
  shared_identifiers: SharedIdentifier[];
  shared_addresses: SharedAddress[];
  knows_relationships: KnowsRelationship[];
}

export interface SalesProduct {
  display_name: string | null;
  sku: string | null;
}

export interface SalesLineItem {
  line_no: number | null;
  quantity: number | null;
  unit_price: number | null;
  subtotal: number | null;
  product: SalesProduct | null;
}

export interface SalesOrder {
  order_no: string | null;
  source_order_id: string | null;
  order_date: string | null;
  total_amount: number | null;
  currency: string | null;
  source_system: string | null;
  entity_name: string | null;
  line_items: SalesLineItem[];
}

export interface EntitySummary {
  entity_key: string;
  display_name: string | null;
  entity_type: string | null;
  country_code: string | null;
  is_active: boolean;
  person_count: number;
  source_record_count: number;
  last_ingested_at: string | null;
  active_review_cases: number;
}

export interface PersonEntitySummary {
  entity_key: string;
  display_name: string | null;
  entity_type: string | null;
  country_code: string | null;
  is_active: boolean;
  source_record_count: number;
}

export interface EntityPerson extends Person {
  phone_confidence: number | null;
}

export interface ListedPerson extends EntityPerson {
  entities: PersonEntitySummary[];
}

// --- Reports (stretchy reports) ---

export type ReportParamType = "string" | "integer" | "float" | "date" | "boolean";

export interface ReportParameterDef {
  name: string;
  label: string;
  param_type: ReportParamType;
  required: boolean;
  default_value: string | null;
}

export interface ReportSummary {
  report_key: string;
  display_name: string;
  description: string | null;
  category: string | null;
}

export interface ReportDetail extends ReportSummary {
  cypher_query: string;
  parameters: ReportParameterDef[];
  created_at: string;
  updated_at: string;
}

export interface ReportResult {
  columns: string[];
  rows: Record<string, string | number | boolean | null>[];
  row_count: number;
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
