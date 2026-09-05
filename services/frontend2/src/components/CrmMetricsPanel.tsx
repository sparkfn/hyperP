"use client";

import { useQuery } from "@tanstack/react-query";
import Bolt from "@mui/icons-material/Bolt";
import ChatBubbleOutline from "@mui/icons-material/ChatBubbleOutline";
import Handshake from "@mui/icons-material/Handshake";
import PhoneInTalk from "@mui/icons-material/PhoneInTalk";
import WorkOutline from "@mui/icons-material/WorkOutline";
import Button from "@mui/material/Button";
import React, { useEffect, useMemo, useState, type ReactElement } from "react";

import { bffFetch, BffError } from "@/lib/api-client";
import type {
  PersonCrmActivityMetrics,
  PersonCrmDealMetrics,
} from "@/lib/api-types";
import styles from "./CrmMetricsPanel.module.css";

interface CrmMetricsPanelProps {
  personId: string;
  onTotalLoaded: (total: number) => void;
}

type CombinedCrmMetrics = PersonCrmDealMetrics & {
  activity_count: number | null;
  call_count: number | null;
  activity_kind_breakdown: PersonCrmActivityMetrics extends infer T
    ? T extends { activity_kind_breakdown: infer B } ? B : never : never;
  first_activity_at: string | null;
  first_activity_at_display: string | null;
  last_activity_at: string | null;
  last_activity_at_display: string | null;
  recent_30d_activity_count: number | null;
  recent_30d_call_count: number | null;
  recent_30d_daily_activity_counts: number[];
  recent_30d_daily_call_counts: number[];
  recent_30d_activity_change_pct: number | null;
  recent_30d_call_change_pct: number | null;
  last_crm_touch_at: string | null;
  last_crm_touch_at_display: string | null;
  days_since_last_crm_touch: number | null;
  days_since_last_activity: number | null;
  activity_status: "loading" | "complete" | "partial" | "unavailable";
  activity_reason: string | null;
};

function titleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function metricTotal(metrics: CombinedCrmMetrics): number {
  const liveTotal = metrics.activity_status === "complete"
    ? (metrics.activity_count ?? 0) + (metrics.call_count ?? 0)
    : 0;
  return metrics.deal_count + metrics.conversation_count + liveTotal;
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

function isLaterTimestamp(candidate: string | null, baseline: string | null): boolean {
  if (candidate === null) return false;
  const candidateAt = Date.parse(candidate);
  if (!Number.isFinite(candidateAt)) return false;
  if (baseline === null) return true;
  const baselineAt = Date.parse(baseline);
  return !Number.isFinite(baselineAt) || candidateAt > baselineAt;
}

function composeMetrics(
  deals: PersonCrmDealMetrics,
  activity: PersonCrmActivityMetrics | undefined,
  activityFailed: boolean,
): CombinedCrmMetrics {
  const completeActivity = activity?.status === "complete" ? activity : null;
  const aggregate = activity?.status === "complete" || activity?.status === "partial" ? activity : null;
  const liveIsNewest = completeActivity !== null
    && isLaterTimestamp(completeActivity.last_activity_at, deals.last_graph_crm_touch_at);
  return {
    ...deals,
    entity_breakdown: deals.entity_breakdown,
    activity_count: aggregate?.activity_count ?? null,
    call_count: aggregate?.call_count ?? null,
    activity_kind_breakdown: aggregate?.activity_kind_breakdown ?? [],
    first_activity_at: aggregate?.first_activity_at ?? null,
    first_activity_at_display: aggregate?.first_activity_at_display ?? null,
    last_activity_at: aggregate?.last_activity_at ?? null,
    last_activity_at_display: aggregate?.last_activity_at_display ?? null,
    recent_30d_activity_count: aggregate?.recent_30d_activity_count ?? null,
    recent_30d_call_count: aggregate?.recent_30d_call_count ?? null,
    recent_30d_daily_activity_counts: completeActivity?.recent_30d_daily_activity_counts ?? [],
    recent_30d_daily_call_counts: completeActivity?.recent_30d_daily_call_counts ?? [],
    recent_30d_activity_change_pct: completeActivity?.recent_30d_activity_change_pct ?? null,
    recent_30d_call_change_pct: completeActivity?.recent_30d_call_change_pct ?? null,
    last_crm_touch_at: liveIsNewest
      ? completeActivity.last_activity_at
      : deals.last_graph_crm_touch_at,
    last_crm_touch_at_display: liveIsNewest
      ? completeActivity.last_activity_at_display
      : deals.last_graph_crm_touch_at_display,
    days_since_last_crm_touch: null,
    days_since_last_activity: null,
    activity_status: activity === undefined
      ? activityFailed ? "unavailable" : "loading"
      : activity.status,
    activity_reason: activity?.status === "complete"
      ? null
      : activity?.failure_reason ?? (activityFailed ? "request_failed" : null),
  };
}

export default function CrmMetricsPanel({
  personId,
  onTotalLoaded,
}: CrmMetricsPanelProps): ReactElement {
  const dealsQuery = useQuery<PersonCrmDealMetrics, Error>({
    queryKey: ["person-crm-deal-metrics", personId],
    queryFn: ({ signal }) => bffFetch(`/bff/persons/${encodeURIComponent(personId)}/crm/deal-metrics`, { signal }),
  });
  const activityQuery = useQuery<PersonCrmActivityMetrics, Error>({
    queryKey: ["person-crm-activity-metrics", personId],
    queryFn: ({ signal }) => bffFetch(`/bff/persons/${encodeURIComponent(personId)}/crm/activity-metrics`, { signal }),
  });
  const metrics = useMemo(
    () => (dealsQuery.data
      ? composeMetrics(dealsQuery.data, activityQuery.data, activityQuery.isError)
      : undefined),
    [activityQuery.data, activityQuery.isError, dealsQuery.data],
  );

  useEffect(() => {
    if (metrics === undefined) return;
    onTotalLoaded(metricTotal(metrics));
  }, [metrics, onTotalLoaded]);

  if (dealsQuery.isPending) {
    return (
      <div className={styles.loadingState} role="status" aria-busy="true">
        Loading CRM engagement…
      </div>
    );
  }

  if (dealsQuery.isError) {
    return (
      <div className={styles.errorState} role="alert">
        {errorMessage(dealsQuery.error)}
      </div>
    );
  }

  if (
    metrics === undefined ||
    (metricTotal(metrics) === 0 && metrics.activity_status === "complete")
  ) {
    return <div className={styles.emptyState}>No CRM records on file.</div>;
  }

  return (
    <div className={styles.panel}>
      <section className={styles.overviewSection} aria-label="CRM overview">
        <CrmMetricCards metrics={metrics} />
      </section>
      <LiveActivityStatus
        metrics={metrics}
        activity={activityQuery.data}
        onRetry={() => { void activityQuery.refetch(); }}
      />
      <CrmBreakdowns metrics={metrics} />
      <CrmRecency metrics={metrics} />
    </div>
  );
}

function LiveActivityStatus({
  metrics,
  activity,
  onRetry,
}: {
  metrics: CombinedCrmMetrics;
  activity: PersonCrmActivityMetrics | undefined;
  onRetry: () => void;
}): ReactElement {
  const freshness = activity === undefined
    ? null
    : activity.cache_disposition === "hit"
      ? "cache hit"
      : activity.cache_disposition === "coalesced"
        ? "coalesced request"
        : activity.cache_disposition === "disabled"
          ? "cache disabled"
          : "fresh";
  const fetched = activity?.fetched_at_display ?? activity?.fetched_at;
  const metadata = fetched && freshness ? ` Fetched ${fetched} (${freshness}).` : "";
  const incomplete = metrics.activity_status === "complete"
    ? ""
    : " Incomplete activity series are not charted.";
  return (
    <div className={styles.breakdownEmpty} role="status">
      <span>
        Live activity {metrics.activity_status}
        {metrics.activity_reason ? `: ${metrics.activity_reason}` : ""}.
        {metadata}{incomplete}
      </span>
      {metrics.activity_status === "unavailable" ? (
        <Button
          size="small"
          variant="outlined"
          onClick={onRetry}
          aria-label="Retry live activity metrics"
        >
          Retry live activity
        </Button>
      ) : null}
    </div>
  );
}

function CrmMetricCards({ metrics }: { metrics: CombinedCrmMetrics }): ReactElement {
  const cards: ReadonlyArray<{
    label: string;
    value: number | null;
    periodValue: number | null;
    changePct: number | null;
    Icon: typeof WorkOutline;
    tone: "deals" | "activities" | "calls" | "chats";
  }> = [
    {
      label: "Deals",
      value: metrics.deal_count,
      periodValue: metrics.recent_30d_deal_count,
      changePct: metrics.recent_30d_deal_change_pct,
      Icon: Handshake,
      tone: "deals",
    },
    {
      label: "Activities",
      value: metrics.activity_count,
      periodValue: metrics.recent_30d_activity_count,
      changePct: metrics.recent_30d_activity_change_pct,
      Icon: Bolt,
      tone: "activities",
    },
    {
      label: "Calls",
      value: metrics.call_count,
      periodValue: metrics.recent_30d_call_count,
      changePct: metrics.recent_30d_call_change_pct,
      Icon: PhoneInTalk,
      tone: "calls",
    },
    {
      label: "Chats",
      value: metrics.conversation_count,
      periodValue: metrics.recent_30d_conversation_count,
      changePct: metrics.recent_30d_conversation_change_pct,
      Icon: ChatBubbleOutline,
      tone: "chats",
    },
  ];

  return (
    <div className={styles.metricGrid}>
      <div className={styles.metricCards}>
        {cards.map(({ label, value, periodValue, changePct, Icon, tone }) => {
          const isLive = tone === "activities" || tone === "calls";
          const lowerBound = isLive && metrics.activity_status === "partial";
          const prefix = lowerBound ? "≥" : "";
          return (
          <div className={`${styles.metricItem} ${styles[`metricCard--${tone}`] ?? ""}`} key={label}>
            <div className={styles.metricBody}>
              <div className={styles.metricTop}>
                <span className={styles.metricValue}>
                  {periodValue === null ? "—" : `${prefix}${periodValue}`}
                </span>
                <div className={styles.metricInfo}>
                  <span className={styles.metricLabel}>{label}</span>
                  <span className={styles.metricAllTime}>
                    {value === null ? "Unavailable" : `${prefix}${value} all time`}
                  </span>
                </div>
                <span className={`${styles.metricIcon} ${styles[`metricIcon--${tone}`] ?? ""}`}>
                  <Icon fontSize="small" />
                </span>
              </div>
              {isLive && metrics.activity_status !== "complete" ? null : (
                <DeltaIndicator changePct={changePct} tone={tone} />
              )}
            </div>
          </div>
          );
        })}
      </div>
      <CrmTrendChart metrics={metrics} />
    </div>
  );
}

function CrmTrendChart({ metrics }: { metrics: CombinedCrmMetrics }): ReactElement {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [period, setPeriod] = useState<"daily" | "weekly" | "monthly">("daily");
  const [horizontalZoom, setHorizontalZoom] = useState(1);
  const graphSeries = [
    { label: "Deals", values: aggregateTrend(metrics.recent_30d_daily_deal_counts, period), tone: "deals" },
    { label: "Chats", values: aggregateTrend(metrics.recent_30d_daily_conversation_counts, period), tone: "chats" },
  ] as const;
  const liveSeries = metrics.activity_status === "complete" ? [
    { label: "Activities", values: aggregateTrend(metrics.recent_30d_daily_activity_counts, period), tone: "activities" as const },
    { label: "Calls", values: aggregateTrend(metrics.recent_30d_daily_call_counts, period), tone: "calls" as const },
  ] : [];
  const series = [...graphSeries, ...liveSeries];
  const periodDays = period === "daily" ? 1 : period === "weekly" ? 7 : 30;
  const width = 900;
  const height = 240;
  const chartLeft = 34;
  const chartRight = 34;
  const effectiveZoom = Math.max(1, horizontalZoom);
  const svgWidth = width * effectiveZoom;
  const chartWidth = svgWidth - chartLeft - chartRight;
  const chartTop = 18;
  const chartBottom = 198;
  const chartHeight = chartBottom - chartTop;
  const chartRenderHeight = 220;
  const pointCount = Math.max(...series.map(({ values }) => values.length), 1);
  const max = Math.max(...series.flatMap(({ values }) => values), 1);
  const scaleMax = Math.max(max, Math.ceil(max * 1.25));
  const scaleValues = scaleMax <= 10
    ? Array.from({ length: scaleMax + 1 }, (_, index) => scaleMax - index)
    : [scaleMax, Math.round(scaleMax * 0.75), Math.round(scaleMax * 0.5), Math.round(scaleMax * 0.25), 0]
        .filter((value, index, values) => values.indexOf(value) === index);
  const point = (value: number, index: number): { x: number; y: number } => {
    const x = chartLeft + (index / Math.max(pointCount - 1, 1)) * chartWidth;
    const y = chartBottom - (value / scaleMax) * chartHeight;
    return { x, y };
  };
  const points = (values: ReadonlyArray<number>): string => values.map((value, index) => {
    const { x, y } = point(value, index);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");
  const hoveredDate = hoveredIndex === null ? null : trendDateLabel(hoveredIndex, pointCount, periodDays);

  return (
    <div className={styles.trendChart}>
      <div className={styles.trendChartHeader}>
        <span className={styles.trendChartTitle}>CRM performance</span>
        <label className={styles.chartSelect}>
          <span className={styles.visuallyHidden}>Chart period</span>
          <select value={period} onChange={(event) => { setPeriod(event.target.value as "daily" | "weekly" | "monthly"); setHoveredIndex(null); }}>
            <option value="daily">Daily</option>
            <option value="weekly">Weekly</option>
            <option value="monthly">Monthly</option>
          </select>
          <span aria-hidden="true">⌁</span>
        </label>
        <div className={styles.chartZoom} aria-label="Chart zoom controls">
          <button type="button" onClick={() => setHorizontalZoom((value) => Math.max(1, value - 0.15))} aria-label="Zoom out">−</button>
          <button type="button" onClick={() => setHorizontalZoom(1)} aria-label="Reset zoom">Reset</button>
          <button type="button" onClick={() => setHorizontalZoom((value) => Math.min(2.5, value + 0.15))} aria-label="Zoom in">+</button>
        </div>
      </div>
      <div className={styles.trendPlot}>
        <div className={styles.chartScaleLabels} aria-hidden="true">
          {scaleValues.map((value) => (
              <span key={value} style={{ top: `${(chartTop + ((scaleMax - value) / scaleMax) * chartHeight) * (chartRenderHeight / height)}px` }}>
              {value}
            </span>
          ))}
        </div>
        <div className={`${styles.chartScaleLabels} ${styles.chartScaleLabelsRight}`} aria-hidden="true">
          {scaleValues.map((value) => (
              <span key={value} style={{ top: `${(chartTop + ((scaleMax - value) / scaleMax) * chartHeight) * (chartRenderHeight / height)}px` }}>
              {value}
            </span>
          ))}
        </div>
        <div className={styles.trendScroll}>
        <svg
          viewBox={`0 0 ${svgWidth} ${height}`}
          preserveAspectRatio="none"
          style={{ width: `${effectiveZoom * 100}%` }}
          role="img"
          aria-label="CRM activity trend for the last 30 days"
          onMouseLeave={() => setHoveredIndex(null)}
        >
          {scaleValues.map((value) => {
            const y = chartBottom - (value / scaleMax) * chartHeight;
            return (
              <g key={value}>
                <line className={styles.chartGridLine} x1={chartLeft} x2={svgWidth - chartRight} y1={y} y2={y} />
              </g>
            );
          })}
          {hoveredIndex !== null ? (
            <line
              className={styles.chartHoverLine}
              x1={point(0, hoveredIndex).x}
              x2={point(0, hoveredIndex).x}
              y1={chartTop}
              y2={chartBottom}
            />
          ) : null}
        {series.map(({ label, values, tone }) => (
          <g key={label} className={styles[`chartSeries--${tone}`] ?? ""}>
            <polyline fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" points={points(values)} />
            {values.map((value, index) => {
              const { x, y } = point(value, index);
              return (
                <circle
                  key={`${label}-${index}`}
                  className={styles.chartPoint}
                  cx={x}
                  cy={y}
                  r={hoveredIndex === index || pointCount === 1 ? 4 : 0}
                  tabIndex={0}
                  aria-label={`${label}: ${value} on ${trendDateLabel(index, pointCount, periodDays)}`}
                  onMouseEnter={() => setHoveredIndex(index)}
                  onFocus={() => setHoveredIndex(index)}
                  onBlur={() => setHoveredIndex(null)}
                />
              );
            })}
          </g>
        ))}
          {Array.from({ length: pointCount }, (_, index) => {
            const { x } = point(0, index);
            return <rect key={index} className={styles.chartHitArea} x={x - chartWidth / pointCount / 2} y={chartTop} width={chartWidth / pointCount} height={chartHeight} onMouseEnter={() => setHoveredIndex(index)} />;
          })}
          {hoveredIndex !== null ? series.map(({ label, values, tone }) => {
            const value = values[hoveredIndex] ?? 0;
            const { x, y } = point(value, hoveredIndex);
            return <circle key={`hover-${label}`} className={styles[`chartSeries--${tone}`] ?? ""} cx={x} cy={y} r="4" fill="currentColor" />;
          }) : null}
          {(period === "daily"
            ? Array.from({ length: pointCount }, (_, index) => index)
            : [0, Math.floor((pointCount - 1) / 4), Math.floor((pointCount - 1) / 2), Math.floor((pointCount - 1) * 3 / 4), pointCount - 1]
          ).filter((value, index, values) => values.indexOf(value) === index).map((index) => {
            const { x } = point(0, index);
            return <text key={index} className={styles.chartAxisLabel} x={x} y={height - 8} textAnchor={period === "daily" ? "middle" : index === 0 ? "start" : index === pointCount - 1 ? "end" : "middle"}>{trendDateLabel(index, pointCount, periodDays).replace(/ \d{4}$/, "")}</text>;
          })}
        </svg>
        {hoveredIndex !== null && hoveredDate !== null ? (
          <div
            className={styles.chartTooltip}
            style={{
              left: `${Math.min(82, Math.max(18, ((chartLeft + (hoveredIndex / Math.max(pointCount - 1, 1)) * chartWidth) / svgWidth) * 100))}%`,
            }}
            role="tooltip"
          >
            <strong>{hoveredDate}</strong>
            {series.map(({ label, values, tone }) => <span key={label} className={styles[`chartTooltip--${tone}`] ?? ""}><i />{label}: {values[hoveredIndex] ?? 0}</span>)}
          </div>
        ) : null}
        </div>
      </div>
      <div className={styles.trendLegend}>
        {series.map(({ label, tone }) => <span key={label} className={styles[`trendLegend--${tone}`] ?? ""}><i />{label}</span>)}
      </div>
    </div>
  );
}

function DeltaIndicator({
  changePct,
  tone,
}: {
  changePct: number | null;
  tone: "deals" | "activities" | "calls" | "chats";
}): ReactElement {
  if (changePct === null) {
    return (
      <span
        className={`${styles.delta} ${styles.deltaNeutral} ${styles[`deltaText--${tone}`] ?? ""}`}
      >
        <span className={styles.deltaArrow} aria-hidden="true">
          —
        </span>
        <span>0% vs last 30 days</span>
      </span>
    );
  }
  if (changePct === 0) {
    return (
      <span
        className={`${styles.delta} ${styles.deltaNeutral} ${styles[`deltaText--${tone}`] ?? ""}`}
      >
        <span className={styles.deltaArrow} aria-hidden="true">
          —
        </span>
        <span>0% vs last 30 days</span>
      </span>
    );
  }
  const direction: "up" | "down" = changePct > 0 ? "up" : "down";
  const className = `${styles.delta} ${direction === "up" ? styles.deltaUp : styles.deltaDown} ${styles[`deltaText--${tone}`] ?? ""}`;
  return (
    <span className={className}>
      <span className={styles.deltaArrow} aria-hidden="true">
        {direction === "up" ? "↑" : "↓"}
      </span>
      <span>
        {Math.abs(changePct)}% vs last 30 days
      </span>
    </span>
  );
}

function Sparkline({
  values,
  tone,
}: {
  values: ReadonlyArray<number>;
  tone: "deals" | "activities" | "calls" | "chats";
}): ReactElement {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const width = 88;
  const height = 28;
  const max = values.length > 0 ? Math.max(...values, 1) : 1;
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  const pointCoordinates = values.map((value, index) => {
      const x = index * stepX;
      const y = height - (value / max) * height;
      return { x, y, value };
    });
  const points = pointCoordinates.map(({ x, y }) => `${x.toFixed(2)},${y.toFixed(2)}`).join(" ");
  const stroke = `var(--crm-sparkline-${tone}, currentColor)`;
  return (
    <div className={styles.sparklineWrap}>
      <svg
        className={`${styles.sparkline} ${styles[`sparkline--${tone}`] ?? ""}`}
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label="30-day trend"
        preserveAspectRatio="none"
        onMouseLeave={() => setHoveredIndex(null)}
      >
        <polyline
          fill="none"
          stroke={stroke}
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          points={points}
        />
        {pointCoordinates.map(({ x, y, value }, index) => (
          <circle
            key={`${index}-${value}`}
            cx={x}
            cy={y}
            r="7"
            fill="currentColor"
            fillOpacity="0.12"
            stroke="none"
            tabIndex={0}
            onMouseEnter={() => setHoveredIndex(index)}
            onFocus={() => setHoveredIndex(index)}
            onBlur={() => setHoveredIndex(null)}
          />
        ))}
      </svg>
      {hoveredIndex !== null && pointCoordinates[hoveredIndex] !== undefined ? (
        <span
          className={styles.sparklineTooltip}
          style={{ left: `${(hoveredIndex / Math.max(values.length - 1, 1)) * 100}%` }}
          role="tooltip"
        >
          {trendDateLabel(hoveredIndex, values.length)} · {values[hoveredIndex]}
        </span>
      ) : null}
    </div>
  );
}

function aggregateTrend(values: ReadonlyArray<number>, period: "daily" | "weekly" | "monthly"): ReadonlyArray<number> {
  if (period === "monthly") {
    const monthlyBuckets = new Map<string, number>();
    values.forEach((value, index) => {
      const date = new Date();
      date.setHours(12, 0, 0, 0);
      date.setDate(date.getDate() - (values.length - index - 1));
      const key = `${date.getFullYear()}-${date.getMonth()}`;
      monthlyBuckets.set(key, (monthlyBuckets.get(key) ?? 0) + value);
    });
    return Array.from(monthlyBuckets.values());
  }

  const bucketSize = period === "daily" ? 1 : 7;
  const buckets: number[] = [];
  for (let index = 0; index < values.length; index += bucketSize) {
    buckets.push(values.slice(index, index + bucketSize).reduce((sum, value) => sum + value, 0));
  }
  return buckets;
}

function trendDateLabel(index: number, total: number, daysPerPoint = 1): string {
  const date = new Date();
  date.setHours(12, 0, 0, 0);
  date.setDate(date.getDate() - (total - index - 1) * daysPerPoint);
  return new Intl.DateTimeFormat("en-GB", {
    day: "2-digit",
    month: "short",
    year: "numeric",
  }).format(date);
}

void Sparkline;

function compactElapsedLabel(days: number | null, date: string | null): string {
  if (days === null || days > 7) return displayDate(date);
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  return `${days} days ago`;
}

function CrmRecency({ metrics }: { metrics: CombinedCrmMetrics }): ReactElement {
  const graphTouches: ReadonlyArray<{
    label: string;
    date: string | null;
    days: number | null;
    relative: boolean;
  }> = [
    { label: "Last CRM", date: metrics.last_crm_touch_at_display, days: metrics.days_since_last_crm_touch, relative: true },
    { label: "First deal", date: metrics.first_deal_at_display, days: null, relative: false },
    { label: "Last deal", date: metrics.last_deal_at_display, days: metrics.days_since_last_deal, relative: true },
  ];
  const liveTouches: ReadonlyArray<{
    label: string;
    date: string | null;
    days: number | null;
    relative: boolean;
  }> = metrics.activity_status === "unavailable" || metrics.activity_status === "loading" ? [] : [
    { label: "First activity", date: metrics.first_activity_at_display, days: null, relative: false },
    { label: "Last activity", date: metrics.last_activity_at_display, days: metrics.days_since_last_activity, relative: true },
  ];
  const touches = [...graphTouches, ...liveTouches];

  return (
    <section className={styles.recencySection} aria-labelledby="crm-recency-title">
      <h3 id="crm-recency-title" className={styles.sectionTitle}>Recency</h3>
      <p className={styles.recencyInline}>
        {touches.map(({ label, date, days, relative }, index) => (
          <React.Fragment key={label}>
            {index > 0 ? " · " : ""}
            <span>{label}: </span>
            <strong>{relative ? compactElapsedLabel(days, date) : displayDate(date)}</strong>
          </React.Fragment>
        ))}
      </p>
    </section>
  );
}

function CrmBreakdowns({ metrics }: { metrics: CombinedCrmMetrics }): ReactElement {
  return (
    <section className={styles.breakdownSection} aria-labelledby="crm-breakdowns-title">
      <h3 id="crm-breakdowns-title" className={styles.sectionTitle}>Breakdowns</h3>
      <div className={styles.breakdownGrid}>
        <CrmStageBreakdown metrics={metrics} />
        <CrmActivityBreakdown metrics={metrics} />
        <CrmEntityTable metrics={metrics} />
      </div>
    </section>
  );
}

function CrmStageBreakdown({ metrics }: { metrics: CombinedCrmMetrics }): ReactElement {
  return (
    <section className={styles.breakdownItem} aria-label="Deals">
      <div className={styles.breakdownHeader}>
        <h3 className={styles.breakdownTitle}>
          <Handshake fontSize="inherit" className={styles.cardTitleIcon} />
          Deals
        </h3>
        <span className={styles.breakdownTotal}>{metrics.deal_count} total</span>
      </div>
      {metrics.deal_stage_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>No stage information available.</p>
      ) : (
        <div className={styles.breakdownScroll}>
          <ul className={styles.breakdownList}>
            {metrics.deal_stage_breakdown.map((stage) => (
              <li className={styles.breakdownRow} key={stage.stage_id ?? "unknown"}>
                <span className={styles.breakdownCount}>{stage.count}</span>
                <span>{stage.stage_id ?? "Unknown stage"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function CrmActivityBreakdown({
  metrics,
}: {
  metrics: CombinedCrmMetrics;
}): ReactElement {
  return (
    <section className={styles.breakdownItem} aria-label="Activities">
      <div className={styles.breakdownHeader}>
        <h3 className={styles.breakdownTitle}>
          <Bolt fontSize="inherit" className={styles.cardTitleIcon} />
          Activities
        </h3>
        <span className={styles.breakdownTotal}>
          {metrics.activity_count === null
            ? "Unavailable"
            : `${metrics.activity_status === "partial" ? "≥" : ""}${metrics.activity_count} total`}
        </span>
      </div>
      {metrics.activity_count === null ? (
        <p className={styles.breakdownEmpty}>Live activity data unavailable.</p>
      ) : metrics.activity_kind_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>
          {metrics.activity_status === "complete"
            ? "No activities on record."
            : "No validated activities returned before the read was truncated."}
        </p>
      ) : (
        <div className={styles.breakdownScroll}>
          <ul className={styles.breakdownList}>
            {metrics.activity_kind_breakdown.map((activity) => (
              <li className={styles.activityRow} key={activity.history_kind}>
                <strong className={styles.activityCount}>
                  {metrics.activity_status === "partial" ? "≥" : ""}{activity.count}
                </strong>
                <span className={styles.activityValue}>
                  <span className={styles.activityType}>{titleCase(activity.history_kind)}</span>
                  <span className={styles.activityDate}> - {displayDate(activity.last_event_at_display)}</span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function CrmEntityTable({ metrics }: { metrics: CombinedCrmMetrics }): ReactElement {
  return (
    <section className={`${styles.breakdownItem} ${styles.entitySection}`} aria-labelledby="crm-entity-title">
      <div className={styles.breakdownHeader}>
        <h3 id="crm-entity-title" className={styles.breakdownTitle}>
          <svg className={`${styles.cardTitleIcon} ${styles.entityCardIcon}`} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="2" y="7" width="20" height="14" rx="2" />
            <path d="M16 7V5a2 2 0 0 0-4 0v2M8 7V5a2 2 0 0 0-4 0v2" />
          </svg>
          Entities
        </h3>
        <span className={styles.breakdownTotal}>{metrics.entity_breakdown.length} total</span>
      </div>
      {metrics.entity_breakdown.length === 0 ? (
        <p className={styles.breakdownEmpty}>No entity attribution available.</p>
      ) : (
        <div className={styles.entityScroll}>
          <ul className={styles.entityList}>
            {metrics.entity_breakdown.map((entity) => (
              <li className={styles.entityRow} key={entity.entity_key}>
                <span className={styles.entityName}>
                  {entity.entity_display_name ?? entity.entity_key}
                </span>
                <span className={styles.entitySummary}>
                  {entity.deal_count} deals - {entity.conversation_count} Chats
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
