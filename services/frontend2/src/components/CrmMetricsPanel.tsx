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
      <section aria-labelledby="crm-overview-title">
        <h3 id="crm-overview-title" className={styles.sectionTitle}>CRM overview</h3>
        <CrmMetricCards metrics={query.data} />
      </section>
      <CrmRecency metrics={query.data} />
      <CrmBreakdowns metrics={query.data} />
      <CrmEngagementSpan metrics={query.data} />
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

function elapsedLabel(days: number | null): string {
  if (days === null) return "No recorded touch";
  if (days === 0) return "Today";
  if (days === 1) return "1 day ago";
  return `${days} days ago`;
}

function CrmRecency({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  const recent: ReadonlyArray<readonly [string, number]> = [
    ["Deals", metrics.recent_30d_deal_count],
    ["Activities", metrics.recent_30d_activity_count],
    ["Calls", metrics.recent_30d_call_count],
    ["Chats", metrics.recent_30d_conversation_count],
  ];
  const touches: ReadonlyArray<readonly [string, string | null, number | null]> = [
    ["Last CRM touch", metrics.last_crm_touch_at_display, metrics.days_since_last_crm_touch],
    ["Last deal", metrics.last_deal_at_display, metrics.days_since_last_deal],
    ["Last activity", metrics.last_activity_at_display, metrics.days_since_last_activity],
  ];

  return (
    <section aria-labelledby="crm-recency-title">
      <h3 id="crm-recency-title" className={styles.sectionTitle}>Recency</h3>
      <div className={styles.recencyGrid}>
        <div className={styles.recentCard}>
          <h4 className={styles.cardTitle}>Last 30 days</h4>
          <div className={styles.recentCounts}>
            {recent.map(([label, count]) => (
              <span key={label} className={styles.recentCount}>
                <strong>{count}</strong> {label}
              </span>
            ))}
          </div>
        </div>
        {touches.map(([label, date, days]) => (
          <div className={styles.touchCard} key={label}>
            <h4 className={styles.cardTitle}>{label}</h4>
            <p className={styles.recencyTouch}>
              {displayDate(date)}
              <span>{elapsedLabel(days)}</span>
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function CrmBreakdowns({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  return (
    <section aria-labelledby="crm-breakdowns-title">
      <h3 id="crm-breakdowns-title" className={styles.sectionTitle}>Breakdowns</h3>
      <div className={styles.breakdownGrid}>
        <CrmStageBreakdown metrics={metrics} />
        <CrmActivityBreakdown metrics={metrics} />
      </div>
    </section>
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

function CrmEngagementSpan({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  const dates: ReadonlyArray<readonly [string, string | null]> = [
    ["First deal", metrics.first_deal_at_display],
    ["Last deal", metrics.last_deal_at_display],
    ["First activity", metrics.first_activity_at_display],
    ["Last activity", metrics.last_activity_at_display],
  ];

  return (
    <section className={styles.engagementCard} aria-labelledby="crm-engagement-title">
      <h3 id="crm-engagement-title" className={styles.sectionTitle}>Engagement span</h3>
      <dl className={styles.engagementGrid}>
        {dates.map(([label, value]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{displayDate(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function CrmEntityTable({ metrics }: { metrics: PersonCrmMetrics }): ReactElement {
  return (
    <section className={styles.entityCard} aria-labelledby="crm-entity-title">
      <h3 id="crm-entity-title" className={styles.sectionTitle}>By entity</h3>
      {metrics.entity_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>No entity attribution available.</p>
      ) : (
        <table className={styles.entityTable}>
          <thead>
            <tr>
              <th scope="col" className={styles.entityNameColumn}>Entity</th>
              <th scope="col" className={styles.entityNumericColumn}>Deals</th>
              <th scope="col" className={styles.entityNumericColumn}>Activities</th>
              <th scope="col" className={styles.entityNumericColumn}>Chats</th>
            </tr>
          </thead>
          <tbody>
            {metrics.entity_breakdown.map((entity) => (
              <tr key={entity.entity_key}>
                <td className={styles.entityNameColumn}>
                  {entity.entity_display_name ?? entity.entity_key}
                </td>
                <td className={styles.entityNumericColumn}>{entity.deal_count}</td>
                <td className={styles.entityNumericColumn}>{entity.activity_count}</td>
                <td className={styles.entityNumericColumn}>{entity.conversation_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
