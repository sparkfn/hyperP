export type CrmDealPreset = "any" | "has" | "none" | "custom";

export interface CrmDealRange {
  min: string;
  max: string;
}

export function parseCrmDealRange(params: URLSearchParams): CrmDealRange {
  return {
    min: params.get("crm_deal_count_min") ?? "",
    max: params.get("crm_deal_count_max") ?? "",
  };
}

function isNonnegativeInteger(value: string): boolean {
  return /^\d+$/.test(value);
}

export function isValidCrmDealRange(range: CrmDealRange): boolean {
  if (range.min !== "" && !isNonnegativeInteger(range.min)) return false;
  if (range.max !== "" && !isNonnegativeInteger(range.max)) return false;
  if (range.min === "" || range.max === "") return true;
  return Number(range.min) <= Number(range.max);
}

export function crmDealPreset(range: CrmDealRange): CrmDealPreset {
  if (range.min === "" && range.max === "") return "any";
  if (range.min === "1" && range.max === "") return "has";
  if (range.min === "0" && range.max === "0") return "none";
  return "custom";
}

export function crmDealRangeLabel(range: CrmDealRange): string {
  if (!isValidCrmDealRange(range)) return "CRM Deals: invalid range";
  if (range.min !== "" && range.max !== "") return `CRM Deals: ${range.min}–${range.max}`;
  if (range.min !== "") return `CRM Deals: ${range.min}+`;
  return `CRM Deals: up to ${range.max}`;
}

export function applyCrmDealRange(params: URLSearchParams, range: CrmDealRange): void {
  if (range.min === "") params.delete("crm_deal_count_min");
  else params.set("crm_deal_count_min", range.min);
  if (range.max === "") params.delete("crm_deal_count_max");
  else params.set("crm_deal_count_max", range.max);
}
