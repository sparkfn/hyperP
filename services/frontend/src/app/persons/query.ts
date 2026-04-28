import { DEFAULT_FILTERS, type PersonsFilters } from "@/components/PersonsFilterPanel";
import type { SortField, SortOrder } from "@/components/PersonsListTable";

function isSortField(v: string | null): v is SortField {
  switch (v) {
    case "preferred_full_name":
    case "preferred_phone":
    case "preferred_email":
    case "preferred_dob":
    case "preferred_nric":
    case "source_record_count":
    case "connection_count":
    case "entity_count":
    case "identifier_count":
    case "order_count":
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

function isPresenceFilter(v: string): v is "" | "true" | "false" {
  return v === "" || v === "true" || v === "false";
}

function parsePresence(v: string | null): "" | "true" | "false" {
  const s = v ?? "";
  return isPresenceFilter(s) ? s : "";
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
      entity_key: sp.get("entity_key") ?? "",
      has_address: parsePresence(sp.get("has_address")),
      addr_street: sp.get("addr_street") ?? "",
      addr_unit: sp.get("addr_unit") ?? "",
      addr_city: sp.get("addr_city") ?? "",
      addr_postal: sp.get("addr_postal") ?? "",
      addr_country: sp.get("addr_country") ?? "",
      updated_after: sp.get("updated_after") ?? "",
      updated_before: sp.get("updated_before") ?? "",
      has_dob: parsePresence(sp.get("has_dob")),
      dob_from: sp.get("dob_from") ?? "",
      dob_to: sp.get("dob_to") ?? "",
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
  if (f.entity_key) params.set("entity_key", f.entity_key);
  if (f.has_address) params.set("has_address", f.has_address);
  if (f.addr_street.trim()) params.set("addr_street", f.addr_street.trim());
  if (f.addr_unit.trim()) params.set("addr_unit", f.addr_unit.trim());
  if (f.addr_city.trim()) params.set("addr_city", f.addr_city.trim());
  if (f.addr_postal.trim()) params.set("addr_postal", f.addr_postal.trim());
  if (f.addr_country.trim()) params.set("addr_country", f.addr_country.trim());
  if (f.updated_after) params.set("updated_after", f.updated_after);
  if (f.updated_before) params.set("updated_before", f.updated_before);
  if (f.has_dob) params.set("has_dob", f.has_dob);
  if (f.dob_from) params.set("dob_from", f.dob_from);
  if (f.dob_to) params.set("dob_to", f.dob_to);
  if (state.sortBy !== DEFAULT_STATE.sortBy) params.set("sort_by", state.sortBy);
  if (state.sortOrder !== DEFAULT_STATE.sortOrder) params.set("sort_order", state.sortOrder);
  if (state.pageIndex > 0) params.set("page", String(state.pageIndex + 1));
  if (state.rowsPerPage !== DEFAULT_STATE.rowsPerPage) {
    params.set("rows", String(state.rowsPerPage));
  }
  return params.toString();
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
  if (filters.entity_key) params.set("entity_key", filters.entity_key);
  if (filters.has_address) params.set("has_address", filters.has_address);
  if (filters.addr_street.trim()) params.set("addr_street", filters.addr_street.trim());
  if (filters.addr_unit.trim()) params.set("addr_unit", filters.addr_unit.trim());
  if (filters.addr_city.trim()) params.set("addr_city", filters.addr_city.trim());
  if (filters.addr_postal.trim()) params.set("addr_postal", filters.addr_postal.trim());
  if (filters.addr_country.trim()) params.set("addr_country", filters.addr_country.trim());
  if (filters.updated_after) params.set("updated_after", `${filters.updated_after}T00:00:00`);
  if (filters.updated_before) params.set("updated_before", `${filters.updated_before}T23:59:59`);
  if (filters.has_dob) params.set("has_dob", filters.has_dob);
  if (filters.dob_from) params.set("dob_from", filters.dob_from);
  if (filters.dob_to) params.set("dob_to", filters.dob_to);
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
  if (f.entity_key) count++;
  if (f.has_address) count++;
  if (f.addr_street.trim()) count++;
  if (f.addr_unit.trim()) count++;
  if (f.addr_city.trim()) count++;
  if (f.addr_postal.trim()) count++;
  if (f.addr_country.trim()) count++;
  if (f.updated_after) count++;
  if (f.updated_before) count++;
  if (f.has_dob) count++;
  if (f.dob_from) count++;
  if (f.dob_to) count++;
  return count;
}