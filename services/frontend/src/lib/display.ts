import type { PersonConnection, PersonStatus, SourceRecord } from "./api-types";
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

export function sourcesToItems(data: SourceRecord[] | undefined): CountCardItem[] | undefined {
  if (data === undefined) return undefined;
  return data.map((r) => ({
    primary: r.source_system,
    secondary: `${r.source_record_id} · ${r.record_type} · ${formatDate(r.ingested_at)}`,
    color: r.record_type === "conversation" ? "warning" : "default",
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
