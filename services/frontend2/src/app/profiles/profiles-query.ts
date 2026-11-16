export const STATUS_OPTIONS = [
  { value: "open", label: "Open" },
  { value: "promoted", label: "Promoted" },
  { value: "retired", label: "Retired" },
  { value: "all", label: "All statuses" },
] as const;

export type StatusFilter = (typeof STATUS_OPTIONS)[number]["value"];

export function isStatusFilter(value: string): value is StatusFilter {
  return STATUS_OPTIONS.some((option) => option.value === value);
}

export function profilesBasePath(query: string, status: StatusFilter): string {
  const params = new URLSearchParams();
  if (query.length >= 2) params.set("q", query);
  // "all" must be sent explicitly: the API defaults an absent status to open.
  params.set("status", status);
  return `/bff/profiles?${params.toString()}`;
}
