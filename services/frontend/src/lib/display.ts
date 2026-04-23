import type { PersonConnection, PersonStatus, SalesOrder, SourceRecord } from "./api-types";
import type { PersonIdentifier } from "./api-types-person";
import type { CountCardItem } from "@/components/CountCardsCell";

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

export function formatDate(value: string): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toISOString().slice(0, 10);
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
