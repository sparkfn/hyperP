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

export interface PublicLink {
  token: string;
  expires_at: string;
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
  preferred_race_ethnicity: string | null;
  profile_completeness_score: number;
  golden_profile_computed_at: string | null;
  golden_profile_version: string | null;
  source_record_count: number;
  connection_count: number;
  // Sum of all orders; only returned by the person-detail endpoint (absent on
  // list/entity projections).
  lifetime_value?: number | null;
  // Per-source loyalty-points balances, read through from identity source
  // records. Only on the authenticated person-detail endpoint (null on public).
  loyalty?: LoyaltySummary[] | null;
  // Vehicles owned/bought by the person. Only on the authenticated
  // person-detail endpoint (null on public).
  vehicles?: VehicleSummary[] | null;
  created_at: string;
  updated_at: string;
}

export interface LoyaltySummary {
  source_system: string;
  points: number | null;
  disable_loyalty: boolean | null;
  current_spend_for_points: number | null;
  current_sales_for_discount: number | null;
  observed_at: string | null;
}

export interface VehicleSummary {
  vehicle_id: string;
  product: string | null;
  product_sku: string | null;
  manufacturer: string | null;
  model: string | null;
  lta_tag: string | null;
  serial_number: string | null;
  relationship: "OWNS" | "BOUGHT";
  is_active: boolean | null;
  conflict_flag: boolean | null;
  observed_at: string | null;
}

export interface SalesVehicleSummary {
  vehicle_id: string;
  product: string | null;
  product_sku: string | null;
  normalized_lta_tag: string | null;
  normalized_serial_number: string | null;
  conflict_flag: boolean;
}

export interface NonVehicleLine {
  product_sku: string | null;
  product: string | null;
  merchant: string | null;
  manufacturer: string | null;
  serial_number: string | null;
  quantity: number | null;
  unit_price: number | null;
  total_amount: number | null;
  currency: string | null;
  category: string | null;
  raw: Record<string, string | number | boolean | null>;
}

// Provenance class of a SourceRecord. `identity` / `bankruptcy` /
// `relationship` / `crm_deal` form the person-capable system family;
// `rental_flat` is a place register routed address-only. Mirrors the API
// SourceRecordTypeLiteral / ingestion RecordType. Labels are derived via
// titleCase ("Bankruptcy", "Rental Flat", "Crm Deal").
export type SourceRecordType =
  | "identity"
  | "bankruptcy"
  | "rental_flat"
  | "relationship"
  | "conversation"
  | "sales"
  | "crm_deal"
  | "crm_history"
  | "call";

export interface SourceRecord {
  source_record_pk: string;
  source_system: string;
  source_record_id: string;
  source_record_version: string | null;
  record_type: SourceRecordType;
  extraction_confidence: number | null;
  link_status: string;
  linked_person_id: string | null;
  parent_source_system?: string | null;
  parent_source_record_id?: string | null;
  parent_record_type?: SourceRecordType | null;
  history_family?: "activity" | "stage" | null;
  history_kind?: string | null;
  history_source?: string | null;
  event_category_id?: string | null;
  event_stage_id?: string | null;
  event_stage_semantic_id?: string | null;
  event_at?: string | null;
  history_projection_version?: string | null;
  history_projection_source?: string | null;
  history_projected_at?: string | null;
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
  source_system_key: string | null;
}

export interface KnowsRelationship {
  relationship_label: string | null;
  relationship_category: string;
  source_system_key: string | null;
}

export interface ConnectionSource {
  source_system_key: string;
  entity_display_name: string | null;
}

export interface PersonConnection {
  person_id: string;
  status: string;
  preferred_full_name: string | null;
  hops: number;
  shared_identifiers: SharedIdentifier[];
  shared_addresses: SharedAddress[];
  knows_relationships: KnowsRelationship[];
  connection_sources: ConnectionSource[];
}

export interface SalesProduct {
  display_name: string | null;
  sku: string | null;
  category: string | null;
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
  release_date: string | null;
  total_amount: number | null;
  currency: string | null;
  source_system: string | null;
  entity_name: string | null;
  points_used: number | null;
  points_gained: number | null;
  line_items: SalesLineItem[];
  vehicles: SalesVehicleSummary[];
  non_vehicle_lines: NonVehicleLine[];
}

export interface CrmActivityKindCount {
  history_kind: string;
  count: number;
  last_event_at: string | null;
  last_event_at_display: string | null;
}

export interface CrmDealStageCount {
  stage_id: string | null;
  count: number;
}

export interface CrmEntityBreakdown {
  entity_key: string;
  entity_display_name: string | null;
  deal_count: number;
  activity_count: number;
  conversation_count: number;
}

export interface PersonCrmMetrics {
  deal_count: number;
  deal_stage_breakdown: CrmDealStageCount[];
  first_deal_at: string | null;
  first_deal_at_display: string | null;
  last_deal_at: string | null;
  last_deal_at_display: string | null;
  activity_count: number;
  call_count: number;
  conversation_count: number;
  activity_kind_breakdown: CrmActivityKindCount[];
  first_activity_at: string | null;
  first_activity_at_display: string | null;
  last_activity_at: string | null;
  last_activity_at_display: string | null;
  entity_breakdown: CrmEntityBreakdown[];
  recent_30d_deal_count: number;
  recent_30d_activity_count: number;
  recent_30d_call_count: number;
  recent_30d_conversation_count: number;
  last_crm_touch_at: string | null;
  last_crm_touch_at_display: string | null;
  days_since_last_crm_touch: number | null;
  days_since_last_deal: number | null;
  days_since_last_activity: number | null;
  // 30-day daily trend series (oldest → newest, UTC midnight buckets).
  // Always length 30; days with no events are 0.
  recent_30d_daily_deal_counts: number[];
  recent_30d_daily_activity_counts: number[];
  recent_30d_daily_call_counts: number[];
  recent_30d_daily_conversation_counts: number[];
  // Percentage change vs the prior 30-day window, rounded to int.
  // null when the prior window is empty (no division by zero in the UI).
  recent_30d_deal_change_pct: number | null;
  recent_30d_activity_change_pct: number | null;
  recent_30d_call_change_pct: number | null;
  recent_30d_conversation_change_pct: number | null;
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
export interface EntityMetadata { entity_key: string; display_name: string | null; entity_type: string | null; country_code: string | null; is_active: boolean; }
export interface EntityMetrics { entity_key: string; person_count: number; source_record_count: number; last_ingested_at: string | null; active_review_cases: number; }

export interface EntityFilterOption {
  entity_key: string;
  display_name: string | null;
}

export interface PersonListSummary {
  all_profiles_count: number;
  high_risk_count: number;
  high_value_count: number;
  no_contact_count: number;
  deals_this_month_count: number;
  all_deals_count: number;
}

export interface PersonListCoreSummary {
  all_profiles_count: number;
  high_risk_count: number;
  high_value_count: number;
  no_contact_count: number;
}

export interface PersonListCrmSummary {
  deals_this_month_count: number;
  all_deals_count: number;
}

export interface SourceSystemSummary {
  source_key: string;
  display_name: string | null;
  system_type: string | null;
  is_active: boolean;
  source_record_count: number;
  last_ingested_at: string | null;
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
  entity_count: number;
  identifier_count: number;
  connection_count: number;
  possible_match_count: number;
  system_match_count: number;
  order_count: number;
  crm_deal_count: number;
  bankruptcy_case_count: number;
  // Server-computed DOB presentation — render verbatim, no client-side parsing.
  preferred_dob_display: string;
  preferred_dob_invalid: boolean;
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

export interface DeleteReportResponse {
  status: string;
  report_key: string;
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
