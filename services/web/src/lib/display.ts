import type { PersonStatus } from "./api-types";

export function statusColor(status: PersonStatus | string): "success" | "default" | "warning" {
  if (status === "active") return "success";
  if (status === "merged") return "default";
  return "warning";
}

export function confidenceColor(value: number | null): "success" | "warning" | "error" | "default" {
  if (value === null) return "default";
  if (value >= 0.8) return "success";
  if (value >= 0.5) return "warning";
  return "error";
}
