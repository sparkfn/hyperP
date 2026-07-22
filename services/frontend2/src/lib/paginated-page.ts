import type { ApiResponse } from "./api-types";

export interface PaginatedPage<T> {
  rows: T[];
  total: number | null;
  nextCursor: string | null;
}

export function normalizePaginatedPage<T>(response: ApiResponse<T[]>): PaginatedPage<T> {
  const total = response.meta.total_count ?? null;
  const hasNoResults = total === 0;
  return {
    rows: hasNoResults ? [] : response.data,
    total,
    nextCursor: hasNoResults ? null : response.meta.next_cursor,
  };
}
