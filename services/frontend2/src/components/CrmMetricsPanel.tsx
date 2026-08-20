"use client";

import { useQuery } from "@tanstack/react-query";
import React, { useEffect, type ReactElement } from "react";

import { bffFetch, BffError } from "@/lib/api-client";
import type { PersonCrmMetrics } from "@/lib/api-types";
import styles from "./CrmMetricsPanel.module.css";

interface CrmMetricsPanelProps {
  personId: string;
  onTotalLoaded: (total: number) => void;
}

function titleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function metricTotal(metrics: PersonCrmMetrics): number {
  return (
    metrics.deal_count +
    metrics.activity_count +
    metrics.call_count +
    metrics.conversation_count
  );
}

function errorMessage(error: Error | null): string {
  if (error instanceof BffError && error.status === 404) {
    return "No CRM records on file.";
  }
  return error instanceof BffError
    ? error.message
    : "CRM engagement metrics could not be loaded.";
}

function displayDate(value: string | null): string {
  return value ?? "—";
}

export default function CrmMetricsPanel({
  personId,
  onTotalLoaded,
}: CrmMetricsPanelProps): ReactElement {
  const query = useQuery<PersonCrmMetrics, Error>({
    queryKey: ["person-crm-metrics", personId],
    queryFn: ({ signal }): Promise<PersonCrmMetrics> =>
      bffFetch<PersonCrmMetrics>(
        `/bff/persons/${encodeURIComponent(personId)}/crm/metrics`,
        { signal },
      ),
  });

  useEffect(() => {
    if (query.data === undefined) return;
    onTotalLoaded(metricTotal(query.data));
  }, [onTotalLoaded, query.data]);

  if (query.isPending) {
    return (
      <div className={styles.loadingState} role="status" aria-busy="true">
        Loading CRM engagement…
      </div>
    );
  }

  if (query.isError) {
    return (
      <div className={styles.errorState} role="alert">
        {errorMessage(query.error)}
      </div>
    );
  }

  if (metricTotal(query.data) === 0) {
    return <div className={styles.emptyState}>No CRM records on file.</div>;
  }

  return (
    <div className={styles.panel}>
      <CrmMetricCards metrics={query.data} />
      <CrmBreakdowns metrics={query.data} />
      <CrmDateRange metrics={query.data} />
      <CrmEntityTable metrics={query.data} />
    </div>
  );
}

function CrmMetricCards({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  const cards: ReadonlyArray<readonly [string, number]> = [
    ["Deals", metrics.deal_count],
    ["Activities", metrics.activity_count],
    ["Calls", metrics.call_count],
    ["Chats", metrics.conversation_count],
  ];

  return (
    <div className={styles.metricGrid}>
      {cards.map(([label, value]) => (
        <div className={styles.metricCard} key={label}>
          <span className={styles.metricLabel}>{label}</span>
          <span className={styles.metricValue}>{value}</span>
        </div>
      ))}
    </div>
  );
}

function CrmBreakdowns({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  return (
    <div className={styles.breakdownGrid}>
      <CrmStageBreakdown metrics={metrics} />
      <CrmActivityBreakdown metrics={metrics} />
    </div>
  );
}

function CrmStageBreakdown({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  return (
    <section className={styles.breakdownCard} aria-label="Deal stages">
      <h3 className={styles.breakdownTitle}>Deal stages</h3>
      {metrics.deal_stage_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>No stage information available.</p>
      ) : (
        <ul className={styles.breakdownList}>
          {metrics.deal_stage_breakdown.map((stage) => (
            <li className={styles.breakdownRow} key={stage.stage_id ?? "unknown"}>
              <span>{stage.stage_id ?? "Unknown stage"}</span>
              <span className={styles.breakdownCount}>{stage.count}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CrmActivityBreakdown({
  metrics,
}: {
  metrics: PersonCrmMetrics;
}): ReactElement {
  return (
    <section className={styles.breakdownCard} aria-label="Activity breakdown">
      <h3 className={styles.breakdownTitle}>Activity breakdown</h3>
      {metrics.activity_kind_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>No activities on record.</p>
      ) : (
        <ul className={styles.breakdownList}>
          {metrics.activity_kind_breakdown.map((activity) => (
            <li className={styles.breakdownRow} key={activity.history_kind}>
              <span>{titleCase(activity.history_kind)}</span>
              <span className={styles.breakdownMeta}>
                {activity.count} · Last{" "}
                {displayDate(activity.last_event_at_display)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function CrmDateRange({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  return (
    <p className={styles.dateRange}>
      First deal {displayDate(metrics.first_deal_at_display)} · Last deal{" "}
      {displayDate(metrics.last_deal_at_display)} · First activity{" "}
      {displayDate(metrics.first_activity_at_display)} · Last activity{" "}
      {displayDate(metrics.last_activity_at_display)}
    </p>
  );
}

function CrmEntityTable({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  return (
    <section className={styles.entityCard} aria-label="CRM by entity">
      <h3 className={styles.breakdownTitle}>By entity</h3>
      {metrics.entity_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>No entity attribution available.</p>
      ) : (
        <table className={styles.entityTable}>
          <thead>
            <tr>
              <th scope="col">Entity</th>
              <th scope="col">Deals</th>
              <th scope="col">Activities</th>
              <th scope="col">Chats</th>
            </tr>
          </thead>
          <tbody>
            {metrics.entity_breakdown.map((entity) => (
              <tr key={entity.entity_key}>
                <td>{entity.entity_display_name ?? entity.entity_key}</td>
                <td>{entity.deal_count}</td>
                <td>{entity.activity_count}</td>
                <td>{entity.conversation_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
