import type { components } from "./api-schema";

type CreateIngestRunRequest = components["schemas"]["CreateIngestRunRequest"];
type EntityFilterOption = components["schemas"]["EntityFilterOption"];
type ListedPerson = components["schemas"]["ListedPerson"];
type PersonCrmDealMetrics = components["schemas"]["PersonCrmDealMetrics"];
type IsRequired<T, K extends keyof T> = Record<never, never> extends Pick<T, K>
  ? false
  : true;
type Assert<T extends true> = T;

export type GeneratedResponseDefaultsStayRequired = [
  Assert<IsRequired<PersonCrmDealMetrics, "recent_30d_deal_count">>,
  Assert<IsRequired<PersonCrmDealMetrics, "last_graph_crm_touch_at">>,
  Assert<IsRequired<components["schemas"]["PersonComparisonEntity"], "entity_kind">>,
  Assert<IsRequired<components["schemas"]["SalesVehicleSummary"], "conflict_flag">>,
];

const requestUsingBackendDefaults: CreateIngestRunRequest = {
  run_type: "manual",
};

const entityFilterOptionWithNullLabel: EntityFilterOption = {
  entity_key: "legacy",
  display_name: null,
};

const listedPersonWithListFields: ListedPerson = {
  person_id: "person-1",
  status: "active",
  is_high_value: false,
  is_high_risk: false,
  profile_completeness_score: 0,
  source_record_count: 0,
  connection_count: 0,
  created_at: "",
  updated_at: "",
  entities: [],
  entity_count: 0,
  identifier_count: 0,
  possible_match_count: 0,
  system_match_count: 0,
  order_count: 0,
  crm_deal_count: 0,
  bankruptcy_case_count: 0,
  preferred_dob_display: "?",
  preferred_dob_invalid: false,
};

void requestUsingBackendDefaults;
void entityFilterOptionWithNullLabel;
void listedPersonWithListFields;
