import type { PersonConnection, PersonStatus, SalesOrder, SourceRecord } from "./api-types";
import type { PersonIdentifier } from "./api-types-person";
import type { CountCardItem } from "@/components/CountCardsCell";

export const APP_TZ = process.env.NEXT_PUBLIC_TZ ?? "UTC";

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

export function formatDate(value: string | null): string {
  if (!value) return "—";
  // Date-only ISO strings represent calendar days and must not be shifted by a display timezone.
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return formatDob(value);
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: APP_TZ });
}

export function formatDob(value: string | null): string {
  if (!value) return "—";
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const parsed = new Date(value + "T00:00:00");
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

export function getInitials(name: string | null, fallback = "?"): string {
  if (!name) return fallback.slice(0, 2).toUpperCase();
  return name.split(/\s+/).filter(Boolean).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
}

export const AVATAR_COLORS = ["#4361ee", "#7c3aed", "#0891b2", "#059669", "#d97706", "#dc2626"] as const;

export function avatarColor(name: string): string {
  return AVATAR_COLORS[name.charCodeAt(0) % AVATAR_COLORS.length] ?? "#4361ee";
}

export function completenessColor(score: number): string {
  if (score >= 0.8) return "#22c55e";
  if (score >= 0.5) return "#f59e0b";
  return "#ef4444";
}

export function statusHex(status: string): string {
  if (status === "active") return "#22c55e";
  if (status === "merged") return "#3b82f6";
  return "#94a3b8";
}

const ENTITY_COLORS = ["#4361ee", "#7c3aed", "#0891b2", "#059669", "#d97706", "#0f766e", "#b45309", "#be185d"] as const;

export function entityColor(key: string): string {
  let h = 0;
  for (let i = 0; i < key.length; i++) h = (h * 31 + key.charCodeAt(i)) >>> 0;
  return ENTITY_COLORS[h % ENTITY_COLORS.length] ?? "#4361ee";
}

export function isOverdue(iso: string | null): boolean {
  return iso !== null && new Date(iso) < new Date();
}

export function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return formatDate(iso);
}

export function connectionsToItems(data: PersonConnection[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((c) => ({
    primary: c.preferred_full_name ?? c.person_id,
    secondary: describeConnection(c),
    color: "info",
  }));
}

export function identifiersToItems(data: PersonIdentifier[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((id) => ({
    primary: `${id.identifier_type}: ${id.normalized_value}`,
    secondary: [
      id.is_active ? "active" : "inactive",
      id.is_verified ? "verified" : "unverified",
      id.source_system_key ?? "",
    ].filter(Boolean).join(" · "),
    color: id.is_active ? "default" : "warning",
  }));
}

export function sourcesToItems(data: SourceRecord[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((r) => ({
    primary: r.source_system,
    secondary: `${r.source_record_id} · ${r.record_type} · ${formatDate(r.ingested_at)}`,
    color: r.record_type === "conversation" ? "warning" : "default",
  }));
}

export function ordersToItems(data: SalesOrder[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((o) => ({
    primary: o.order_no ?? o.source_order_id ?? "—",
    secondary: [
      o.release_date ? formatDate(o.release_date) : null,
      o.total_amount !== null ? `${o.currency ?? "SGD"} ${o.total_amount.toFixed(2)}` : null,
      o.line_items.length > 0 ? `${o.line_items.length} item${o.line_items.length !== 1 ? "s" : ""}` : null,
      o.entity_name ?? o.source_system ?? null,
    ].filter(Boolean).join(" · "),
    color: "info",
  }));
}

function describeConnection(c: PersonConnection): string {
  const parts: string[] = [];
  for (const si of c.shared_identifiers) {
    parts.push(`${si.identifier_type}:${si.normalized_value}`);
  }
  for (const sa of c.shared_addresses) {
    parts.push(`address:${sa.normalized_full ?? sa.address_id}`);
  }
  for (const kr of c.knows_relationships) {
    parts.push(`knows:${kr.relationship_label ?? kr.relationship_category}`);
  }
  return parts.join(" · ");
}
