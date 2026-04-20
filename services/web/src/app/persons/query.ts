import type { PersonsFilters, TriState } from "@/components/PersonsFilterPanel";
import type { SortField, SortOrder } from "@/components/PersonsListTable";

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
