import type { components } from "./api-schema";

type CreateIngestRunRequest = components["schemas"]["CreateIngestRunRequest"];
type IsRequired<T, K extends keyof T> = Record<never, never> extends Pick<T, K>
  ? false
  : true;
type Assert<T extends true> = T;

export type GeneratedResponseDefaultsStayRequired = [
  Assert<IsRequired<components["schemas"]["PersonComparisonEntity"], "entity_kind">>,
  Assert<IsRequired<components["schemas"]["SalesVehicleSummary"], "conflict_flag">>,
];

const requestUsingBackendDefaults: CreateIngestRunRequest = {
  run_type: "manual",
};

void requestUsingBackendDefaults;
