const NON_FILTER_QUERY_KEYS = new Set([
  "cursor",
  "include_total",
  "limit",
  "sort_by",
  "sort_order",
]);

export function hasEffectivePersonListFilters(query: string): boolean {
  const params = new URLSearchParams(query);
  for (const key of params.keys()) {
    if (!NON_FILTER_QUERY_KEYS.has(key)) return true;
  }
  return false;
}

interface PaginationShowingOptions {
  rowCount: number;
  pageIndex: number;
  pageSize: number;
  exactTotal: number | null;
  unfilteredSummaryTotal: number | null;
  hasEffectiveFilters: boolean;
}

export function formatPaginationShowing({
  rowCount,
  pageIndex,
  pageSize,
  exactTotal,
  unfilteredSummaryTotal,
  hasEffectiveFilters,
}: PaginationShowingOptions): string {
  const start = rowCount === 0 ? 0 : pageIndex * pageSize + 1;
  const end = rowCount === 0 ? 0 : pageIndex * pageSize + rowCount;
  const displayTotal = exactTotal ?? (hasEffectiveFilters ? null : unfilteredSummaryTotal);
  const denominator = displayTotal == null ? "" : ` of ${displayTotal.toLocaleString()}`;
  return `Showing ${start}\u2013${end}${denominator} profiles`;
}
