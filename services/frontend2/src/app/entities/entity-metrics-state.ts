import type { EntityMetrics } from "@/lib/api-types";

export function metricsByEntity(metrics: EntityMetrics[]): ReadonlyMap<string, EntityMetrics> {
  return new Map(metrics.map((metric) => [metric.entity_key, metric]));
}
