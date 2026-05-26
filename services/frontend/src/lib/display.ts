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

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  const formatted = parsed.toLocaleString("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  });
  return formatted.replace("am", "am").replace("pm", "pm") as string;
}

export function formatDob(value: string | null): string {
  if (!value) return "—";
  const datePart = value.split("T", 1)[0];
  if (datePart !== undefined && /^\d{4}-\d{2}-\d{2}$/.test(datePart)) {
    const parsed = new Date(datePart + "T00:00:00");
    if (!Number.isNaN(parsed.getTime())) {
      return parsed.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    }
  }
  if (/^\d{2}-\d{2}-\d{2}$/.test(value)) {
    const [yy, mm, dd] = value.split("-");
    const parsed = new Date(`19${yy}-${mm}-${dd}T00:00:00`);
    if (!Number.isNaN(parsed.getTime())) {
      return parsed.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    }
  }
  return value;
}
