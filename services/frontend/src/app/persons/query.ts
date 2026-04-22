import { DEFAULT_FILTERS, type PersonsFilters, type TriState } from "@/components/PersonsFilterPanel";
import type { SortField, SortOrder } from "@/components/PersonsListTable";

function isSortField(v: string | null): v is SortField {
  switch (v) {
    case "preferred_full_name":
    case "status":
    case "preferred_phone":
    case "preferred_email":
    case "preferred_dob":
    case "preferred_nric":
    case "source_record_count":
    case "connection_count":
    case "updated_at":
    case "profile_completeness_score":
      return true;
    default:
      return false;
  }
}

function isSortOrder(v: string | null): v is SortOrder {
  return v === "asc" || v === "desc";
}

function isTriState(v: string | null): v is TriState {
  return v === "any" || v === "true" || v === "false";
}

function parseTriState(v: string | null): TriState {
  return isTriState(v) ? v : "any";
}

function parsePositiveInt(v: string | null, fallback: number): number {
  if (v === null) return fallback;
  const n = parseInt(v, 10);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

export interface PersonsListState {
  filters: PersonsFilters;
  sortBy: SortField;
  sortOrder: SortOrder;
  pageIndex: number;
  rowsPerPage: number;
}

export const DEFAULT_STATE: PersonsListState = {
  filters: DEFAULT_FILTERS,
  sortBy: "profile_completeness_score",
  sortOrder: "desc",
  pageIndex: 0,
  rowsPerPage: 25,
};

interface ParseOptions {
  validRowsPerPage: readonly number[];
}

export function parseStateFromParams(
  sp: URLSearchParams | { get: (k: string) => string | null },
  opts: ParseOptions,
): PersonsListState {
  const rowsRaw = parsePositiveInt(sp.get("rows"), DEFAULT_STATE.rowsPerPage);
  const rowsPerPage = opts.validRowsPerPage.includes(rowsRaw)
    ? rowsRaw
    : DEFAULT_STATE.rowsPerPage;
  const pageRaw = parsePositiveInt(sp.get("page"), 1);
  const sortByRaw = sp.get("sort_by");
  const sortOrderRaw = sp.get("sort_order");
  return {
    filters: {
      q: sp.get("q") ?? "",
      status: sp.get("status") ?? "",
      entity_key: sp.get("entity_key") ?? "",
      is_high_value: parseTriState(sp.get("is_high_value")),
      is_high_risk: parseTriState(sp.get("is_high_risk")),
      has_phone: parseTriState(sp.get("has_phone")),
      has_email: parseTriState(sp.get("has_email")),
      updated_after: sp.get("updated_after") ?? "",
      updated_before: sp.get("updated_before") ?? "",
    },
    sortBy: isSortField(sortByRaw) ? sortByRaw : DEFAULT_STATE.sortBy,
    sortOrder: isSortOrder(sortOrderRaw) ? sortOrderRaw : DEFAULT_STATE.sortOrder,
    pageIndex: Math.max(0, pageRaw - 1),
    rowsPerPage,
  };
}

export function serializeStateToParams(state: PersonsListState): string {
  const params = new URLSearchParams();
  const f = state.filters;
  if (f.q.trim()) params.set("q", f.q.trim());
  if (f.status) params.set("status", f.status);
  if (f.entity_key) params.set("entity_key", f.entity_key);
  if (f.is_high_value !== "any") params.set("is_high_value", f.is_high_value);
  if (f.is_high_risk !== "any") params.set("is_high_risk", f.is_high_risk);
  if (f.has_phone !== "any") params.set("has_phone", f.has_phone);
  if (f.has_email !== "any") params.set("has_email", f.has_email);
  if (f.updated_after) params.set("updated_after", f.updated_after);
  if (f.updated_before) params.set("updated_before", f.updated_before);
  if (state.sortBy !== DEFAULT_STATE.sortBy) params.set("sort_by", state.sortBy);
  if (state.sortOrder !== DEFAULT_STATE.sortOrder) params.set("sort_order", state.sortOrder);
  if (state.pageIndex > 0) params.set("page", String(state.pageIndex + 1));
  if (state.rowsPerPage !== DEFAULT_STATE.rowsPerPage) {
    params.set("rows", String(state.rowsPerPage));
  }
  return params.toString();
}

function triStateToQuery(v: TriState): string | null {
  return v === "any" ? null : v;
}

function offsetToCursor(offset: number): string | null {
  return offset <= 0 ? null : btoa(String(offset));
}

export function buildQuery(
  filters: PersonsFilters,
  sortBy: SortField,
  sortOrder: SortOrder,
  pageIndex: number,
  rowsPerPage: number,
): string {
  const params = new URLSearchParams();
  if (filters.q.trim().length >= 3) params.set("q", filters.q.trim());
  if (filters.status) params.set("status", filters.status);
  if (filters.entity_key) params.set("entity_key", filters.entity_key);
  const hv = triStateToQuery(filters.is_high_value);
  if (hv !== null) params.set("is_high_value", hv);
  const hr = triStateToQuery(filters.is_high_risk);
  if (hr !== null) params.set("is_high_risk", hr);
  const hp = triStateToQuery(filters.has_phone);
  if (hp !== null) params.set("has_phone", hp);
  const he = triStateToQuery(filters.has_email);
  if (he !== null) params.set("has_email", he);
  if (filters.updated_after) params.set("updated_after", `${filters.updated_after}T00:00:00`);
  if (filters.updated_before) params.set("updated_before", `${filters.updated_before}T23:59:59`);
  params.set("sort_by", sortBy);
  params.set("sort_order", sortOrder);
  params.set("limit", String(rowsPerPage));
  const cursor = offsetToCursor(pageIndex * rowsPerPage);
  if (cursor) params.set("cursor", cursor);
  return params.toString();
}

export function countActiveFilters(f: PersonsFilters): number {
  let count = 0;
  if (f.q.trim().length >= 3) count++;
  if (f.status) count++;
  if (f.entity_key) count++;
  if (f.is_high_value !== "any") count++;
  if (f.is_high_risk !== "any") count++;
  if (f.has_phone !== "any") count++;
  if (f.has_email !== "any") count++;
  if (f.updated_after) count++;
  if (f.updated_before) count++;
  return count;
}
