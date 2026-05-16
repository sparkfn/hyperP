"use client";

import { Fragment, use, useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import Link from "next/link";
import { notFound, useSearchParams } from "next/navigation";
import type { Person, PersonConnection, SalesOrder } from "@/lib/api-types";
import type {
  ManualMergeRequestBody,
  ManualMergeResponseBody,
  PersonAuditEvent,
  PersonBankruptcyCase,
  PersonIdentifier,
  PersonMatchDecision,
  PersonSourceRecord,
  SurvivorshipOverrideRequestBody,
} from "@/lib/api-types-person";
import { bffFetch, BffError, bffFetchEnvelope } from "@/lib/api-client";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";
import type { PublicLink } from "@/lib/api-types";
import PersonFocusedGraph from "@/components/PersonFocusedGraph";
import PersonGraphDialog from "@/components/PersonGraphDialog";
import styles from "./person.module.css";

const AVATAR_COLORS = ["#4361ee", "#7c3aed", "#0891b2", "#059669", "#d97706", "#dc2626"];

type Tab = "timeline" | "matches" | "connections" | "identifier" | "source" | "sales" | "bankruptcy" | "audit" | "graph";

type DetailData = {
  identifiers: PersonIdentifier[];
  sourceRecords: PersonSourceRecord[];
  sales: SalesOrder[];
  audit: PersonAuditEvent[];
};


type SnapshotField = {
  label: string;
  value: string;
  mono?: boolean;
};

type TabConfig = {
  id: Tab;
  label: string;
  count?: number;
};

function avatarColor(name: string): string {
  return AVATAR_COLORS[name.charCodeAt(0) % AVATAR_COLORS.length] ?? "#4361ee";
}

function scoreColor(score: number): string {
  if (score >= 0.8) return "#22c55e";
  if (score >= 0.5) return "#f59e0b";
  return "#ef4444";
}

const DATE_FORMATTER = new Intl.DateTimeFormat("en-SG", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

const DATE_TIME_FORMATTER = new Intl.DateTimeFormat("en-SG", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: true,
  timeZone: "UTC",
});

function fmtDate(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "—";
  return DATE_FORMATTER.format(d);
}

function fmtDateTime(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "—";
  return DATE_TIME_FORMATTER.format(d);
}

function fmtTime(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-SG", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
    timeZone: "UTC",
  }).format(d);
}

function fmtCurrency(amount: number | null, currency: string | null): string {
  if (amount == null) return "—";
  return `${currency ?? "SGD"} ${amount.toFixed(2)}`;
}

function titleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function decisionBadgeClass(decision: string): string | undefined {
  if (decision === "merge") return styles.matchDecisionMerge;
  if (decision === "review") return styles.matchDecisionReview;
  if (decision === "no_match") return styles.matchDecisionNoMatch;
  return styles.matchDecisionDefault;
}

function personInitials(name: string | null): string {
  return (name ?? "?")
    .split(" ")
    .filter(Boolean)
    .map((part) => part[0])
    .slice(0, 2)
    .join("")
    .toUpperCase();
}

function TabPagination({ from, to, total, hasPrev, hasNext, onPrev, onNext }: {
  from: number; to: number; total: number | null;
  hasPrev: boolean; hasNext: boolean;
  onPrev: () => void; onNext: () => void;
}): ReactElement {
  return (
    <div className={styles.tabPagination}>
      <button type="button" className={styles.tabPagBtn} onClick={onPrev} disabled={!hasPrev}>‹ Prev</button>
      <span className={styles.tabPagInfo}>{from}–{to}{total !== null ? ` of ${total}` : ""}</span>
      <button type="button" className={styles.tabPagBtn} onClick={onNext} disabled={!hasNext}>Next ›</button>
    </div>
  );
}

function PersonBreadcrumb({ personName, onShare, shareLoading, onOverride, onMerge }: { personName: string | null; onShare: () => void; shareLoading: boolean; onOverride: () => void; onMerge: () => void }): ReactElement {
  return (
    <div className={styles.breadcrumbRow}>
      <div className={styles.breadcrumb}>
        <Link href="/persons">Persons</Link>
        <span className={styles.breadcrumbSep}>/</span>
        <span className={styles.breadcrumbCurrent}>{personName ?? "Unknown person"}</span>
      </div>
      <div className={styles.breadcrumbActions}>
        <button type="button" className={styles.bcBtnText} onClick={onShare} disabled={shareLoading}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/>
          </svg>
          {shareLoading ? "Generating…" : "Share"}
        </button>
        <button type="button" className={styles.bcBtnOutline} onClick={onOverride}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
          </svg>
          Override fields
        </button>
        <button type="button" className={styles.bcBtnDanger} onClick={onMerge}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M17 20.41L18.41 19 15 15.59 13.59 17 17 20.41zM7.5 8H11v5.59L5.59 19 7 20.41l6-6V8h3.5L12 3.5 7.5 8z"/>
          </svg>
          Merge into
        </button>
      </div>
    </div>
  );
}

function PersonSidebar({ person, detailData }: { person: Person; detailData: DetailData }): ReactElement {
  const completeness = Math.round(person.profile_completeness_score * 100);
  const statusClass =
    person.status === "active"
      ? styles.badgeActive
      : person.status === "merged"
        ? styles.badgeMerged
        : styles.badgeSuppressed;

  const priorityLabel = person.is_high_risk
    ? { label: "Risk", className: styles.priorityRisk }
    : person.is_high_value
      ? { label: "High", className: styles.priorityHigh }
      : person.profile_completeness_score < 0.3
        ? { label: "Low", className: styles.priorityLow }
        : null;

  const summaryFields: SnapshotField[] = [
    { label: "NRIC", value: person.preferred_nric ?? "—", mono: true },
    { label: "DOB", value: person.preferred_dob ?? "—" },
    { label: "Source record", value: String(person.source_record_count) },
    { label: "Updated", value: fmtDateTime(person.updated_at) },
  ];

  const phones = Array.from(
    new Set(
      [
        person.preferred_phone,
        ...detailData.identifiers
          .filter((identifier) => identifier.identifier_type === "phone")
          .map((identifier) => identifier.normalized_value),
        ...detailData.sourceRecords.flatMap((record) =>
          (record.normalized_payload?.identifiers ?? [])
            .filter((identifier) => identifier.identifier_type === "phone")
            .map((identifier) => identifier.normalized_value),
        ),
      ].filter((value): value is string => Boolean(value)),
    ),
  );

  const emails = Array.from(
    new Set(
      [
        person.preferred_email,
        ...detailData.identifiers
          .filter((identifier) => identifier.identifier_type === "email")
          .map((identifier) => identifier.normalized_value),
        ...detailData.sourceRecords.flatMap((record) =>
          (record.normalized_payload?.identifiers ?? [])
            .filter((identifier) => identifier.identifier_type === "email")
            .map((identifier) => identifier.normalized_value),
        ),
      ].filter((value): value is string => Boolean(value)),
    ),
  );

  const addresses = Array.from(
    new Set(
      [
        person.preferred_address?.normalized_full,
        ...detailData.sourceRecords
          .map((record) => record.normalized_payload?.address?.normalized_full)
          .filter((value): value is string => Boolean(value)),
      ].filter((value): value is string => Boolean(value)),
    ),
  );

  return (
    <aside className={styles.sidebar}>
      <section className={styles.sidebarHeroCard}>
        <div className={styles.sidebarHeroTop}>
          <div className={styles.sidebarHeroAvatarCol}>
            <div
              className={styles.avatarRing}
              style={{ background: `conic-gradient(${scoreColor(person.profile_completeness_score)} ${completeness}%, rgba(148, 163, 184, 0.18) 0)` }}
            >
              <div className={styles.avatarRingInner}>
                <div className={styles.avatar} style={{ background: avatarColor(person.preferred_full_name ?? "?") }}>
                  {personInitials(person.preferred_full_name)}
                </div>
              </div>
            </div>
          </div>
          <div className={styles.sidebarHeroIdentity}>
            <div className={styles.sidebarHeroRow}>
              <div className={styles.sidebarHeroName}>{person.preferred_full_name}</div>
            </div>
            <div className={styles.sidebarHeroMetaRow}>
              <div className={`${styles.sidebarHeroMetaLine} ${styles.sidebarHeroPersonId}`}>{person.person_id}</div>
              <span className={`${styles.badge} ${statusClass} ${styles.badgeCompact} ${styles.badgeSubtle}`}>
                {titleCase(person.status)}
              </span>
            </div>
            <div className={styles.compactCompletenessInline}>
              <span className={styles.compactCompletenessValue}>{completeness}%</span>
              <div className={styles.completenessBarWrap}>
                <div className={styles.completenessBarCompact}>
                  <div
                    className={styles.completenessFill}
                    style={{ width: `${completeness}%`, background: scoreColor(person.profile_completeness_score) }}
                  />
                </div>
              </div>
              {priorityLabel && <span className={`${styles.priorityLabel} ${priorityLabel.className}`}>{priorityLabel.label}</span>}
            </div>
          </div>
        </div>
        <div className={styles.sidebarHeroSummaryRows}>
          {summaryFields.map((field) => (
            <div key={field.label} className={styles.sidebarHeroSummaryRow}>
              <div className={styles.sidebarHeroSummaryLabel}>{field.label}</div>
              <div className={`${styles.sidebarHeroSummaryValue} ${field.mono ? styles.mono : ""}`}>{field.value}</div>
            </div>
          ))}
          {(() => {
            const phoneItems = phones.length ? phones : ["—"];
            return phoneItems.map((phone, index) => (
              <div key={`phone-${phone}-${index}`} className={styles.sidebarHeroSummaryRow}>
                <div className={styles.sidebarHeroSummaryLabel}>{`Phone ${index + 1}`}</div>
                <div className={`${styles.sidebarHeroSummaryValue} ${styles.infoValueItem}`}>{phone}</div>
              </div>
            ));
          })()}
          {(() => {
            const emailItems = emails.length ? emails : ["—"];
            return emailItems.map((email, index) => (
              <div key={`email-${email}-${index}`} className={styles.sidebarHeroSummaryRow}>
                <div className={styles.sidebarHeroSummaryLabel}>{`Email ${index + 1}`}</div>
                <div className={`${styles.sidebarHeroSummaryValue} ${styles.infoValueItem}`}>{email}</div>
              </div>
            ));
          })()}
        </div>
      </section>
      <section className={styles.sidebarCard}>
        <div className={`${styles.infoCardTitle} ${styles.addressCardTitle}`}>Addresses</div>
        <div className={styles.addressList}>
          {(addresses.length ? addresses : ["—"]).map((address) => (
            <div key={address} className={styles.addressItem}>
              <div className={styles.addressItemValue}>{address}</div>
            </div>
          ))}
        </div>
      </section>
    </aside>
  );
}

function PersonTabs({ tabs, activeTab, onChange }: { tabs: TabConfig[]; activeTab: Tab; onChange: (tab: Tab) => void }): ReactElement {
  return (
    <div className={styles.tabs}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className={`${styles.tab} ${activeTab === tab.id ? styles.tabActive : ""}`}
          onClick={() => onChange(tab.id)}
        >
          <span>{tab.label}</span>
          {!!tab.count && <span className={styles.tabCount}>{tab.count}</span>}
        </button>
      ))}
    </div>
  );
}

interface TopStat {
  label: string;
  value: string;
  note: string;
  valueStyle?: { color: string };
}

function RightRail({ person, detailData, salesTotal, identifiersTotal, tabs, activeTab, onChange, children }: { person: Person; detailData: DetailData; salesTotal: number | undefined; identifiersTotal: number | undefined; tabs: TabConfig[]; activeTab: Tab; onChange: (tab: Tab) => void; children: ReactElement }): ReactElement {
  const totalSales = detailData.sales.reduce((sum, order) => sum + (order.total_amount ?? 0), 0);
  const completeness = Math.round(person.profile_completeness_score * 100);
  const latestActivityAt = detailData.sourceRecords[0]?.observed_at ?? person.updated_at;
  const salesCount = salesTotal ?? detailData.sales.length;
  const idCount = identifiersTotal ?? detailData.identifiers.length;

  const topStats: TopStat[] = [
    {
      label: "Lifetime value",
      value: detailData.sales.length ? fmtCurrency(totalSales, detailData.sales[0]?.currency ?? "SGD") : "—",
      note: salesCount ? `${salesCount} orders` : "No orders yet",
    },
    {
      label: "Completeness",
      value: `${completeness}%`,
      note: `${idCount} identifiers`,
      valueStyle: { color: scoreColor(person.profile_completeness_score) },
    },
    {
      label: "Connections",
      value: String(person.connection_count),
      note: "linked profiles",
    },
    {
      label: "Last activity",
      value: fmtDate(latestActivityAt),
      note: `at ${fmtTime(person.updated_at)}`,
    },
  ];

  return (
    <div className={styles.mainColumn}>
      <section className={styles.summaryStrip}>
        {topStats.map((stat) => (
          <div key={stat.label} className={styles.summaryCard}>
            <div className={styles.summaryCardLabel}>{stat.label}</div>
            <div className={styles.summaryCardValue} style={stat.valueStyle}>{stat.value}</div>
            <div className={styles.summaryCardNote}>{stat.note}</div>
          </div>
        ))}
      </section>

      <div className={styles.rightTabsInline}>
        <PersonTabs tabs={tabs} activeTab={activeTab} onChange={onChange} />
      </div>

      <div className={styles.tabPanelScroll}>{children}</div>
    </div>
  );
}


type TLEventKind = "audit" | "source" | "sale";

type TLEvent = {
  id: string;
  timestamp: string;
  kind: TLEventKind;
  title: string;
  meta: string;
  description?: string;
};


function buildTimeline(detailData: DetailData): TLEvent[] {
  const events: TLEvent[] = [];

  detailData.audit.forEach((e) => {
    events.push({
      id: `audit-${e.merge_event_id}`,
      timestamp: e.created_at,
      kind: "audit",
      title: e.event_type,
      meta: `${e.actor_type}:${e.actor_id}`,
      description: e.reason ?? undefined,
    });
  });

  detailData.sourceRecords.forEach((r) => {
    events.push({
      id: `src-${r.source_record_pk}`,
      timestamp: r.ingested_at,
      kind: "source",
      title: "record_ingested",
      meta: `${r.source_system} · ${r.source_record_id}`,
    });
    if (r.observed_at !== r.ingested_at) {
      events.push({
        id: `obs-${r.source_record_pk}`,
        timestamp: r.observed_at,
        kind: "source",
        title: "record_observed",
        meta: `${r.source_system} · ${r.source_record_id}`,
      });
    }
  });

  detailData.sales.forEach((o, i) => {
    if (!o.order_date) return;
    events.push({
      id: `sale-${o.order_no ?? i}`,
      timestamp: `${o.order_date}T00:00:00Z`,
      kind: "sale",
      title: "order_placed",
      meta: `${o.order_no ?? o.source_order_id ?? "—"} · ${o.entity_name ?? o.source_system ?? ""}`,
      description: o.total_amount != null ? fmtCurrency(o.total_amount, o.currency) : undefined,
    });
  });

  return events.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime());
}

function TimelineTab({ person, detailData }: { person: Person; detailData: DetailData }): ReactElement {
  const events = useMemo(() => buildTimeline(detailData), [detailData]);
  const [collapsedDates, setCollapsedDates] = useState<Set<string>>(new Set());

  const grouped = useMemo(() => {
    const map = new Map<string, TLEvent[]>();
    events.forEach((e) => {
      const day = e.timestamp.slice(0, 10);
      const arr = map.get(day) ?? [];
      arr.push(e);
      map.set(day, arr);
    });
    return Array.from(map.entries()).map(([day, evts]) => ({ day, evts }));
  }, [events]);

  function toggleDate(day: string): void {
    setCollapsedDates((prev) => {
      const next = new Set(prev);
      if (next.has(day)) next.delete(day); else next.add(day);
      return next;
    });
  }

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Timeline</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{events.length} events</span>
      </div>
      <div className={styles.tlGroups}>
        {grouped.map(({ day, evts }) => {
          const isCollapsed = collapsedDates.has(day);
          return (
            <div key={day} className={styles.tlGroup}>
              <button
                type="button"
                className={styles.tlGroupHeader}
                onClick={() => toggleDate(day)}
              >
                <svg className={`${styles.tlChevron} ${isCollapsed ? "" : styles.tlChevronOpen}`} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                  <path d="M9 18l6-6-6-6" />
                </svg>
                <span className={styles.tlGroupDate}>{fmtDate(day)}</span>
                {isCollapsed && <span className={styles.tlGroupBadge}>{evts.length}</span>}
              </button>
              {!isCollapsed && (
                <div className={styles.tlEventsList}>
                  {evts.map((e) => (
                    <div key={e.id} className={styles.tlEventItem}>
                      <div className={styles.tlEventTop}>
                        <span className={styles.tlTitle}>{e.title}</span>
                        <span className={styles.tlTime}>{fmtTime(e.timestamp)}</span>
                      </div>
                      <div className={styles.tlMeta}>{e.meta}</div>
                      {e.description && <div className={styles.tlDesc}>{e.description}</div>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
        {events.length === 0 && <p className={styles.tlEmpty}>No events recorded.</p>}
      </div>
      <div className={styles.tlFooter}>
        <span className={styles.tlFooterLabel}>Person created</span>
        <span className={styles.tlFooterValue}>{fmtDateTime(person.created_at)}</span>
      </div>
    </section>
  );
}

function DetailShell({ person, detailData, salesTotal, identifiersTotal, children, tabs, activeTab, onTabChange }: { person: Person; detailData: DetailData; salesTotal: number | undefined; identifiersTotal: number | undefined; children: ReactElement; tabs: TabConfig[]; activeTab: Tab; onTabChange: (tab: Tab) => void }): ReactElement {
  return (
    <div className={styles.detailLayout}>
      <PersonSidebar person={person} detailData={detailData} />
      <RightRail person={person} detailData={detailData} salesTotal={salesTotal} identifiersTotal={identifiersTotal} tabs={tabs} activeTab={activeTab} onChange={onTabChange}>
        {children}
      </RightRail>
    </div>
  );
}



function IdTypeIcon({ type }: { type: string }): ReactElement {
  if (type === "phone") return <PhoneIcon />;
  if (type === "email") return <EmailIcon />;
  if (type === "nric") {
    return (
      <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style={{ flexShrink: 0 }}>
        <path d="M20 4H4c-1.11 0-2 .89-2 2v12c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2zm0 14H4V6h16v12zM6 10h2v2H6zm0 4h8v2H6zm4-4h8v2h-8z" />
      </svg>
    );
  }
  return <KeyIcon />;
}

const SKEL_N = [0, 1, 2, 3, 4] as const;

function MergeLoadingOverlay(): ReactElement {
  return (
    <div className={styles.modalLoadingOverlay}>
      <svg className={styles.modalLoadingSpinner} viewBox="0 0 100 100" overflow="visible" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        {/* System A — center (28,50), ring r=12 */}
        <circle cx="28" cy="50" r="12" fill="none" stroke="currentColor" strokeWidth="0.4">
          <animate attributeName="opacity" values="0.22;0.22;0;0;0;0.22;0.22" keyTimes="0;0.28;0.40;0.62;0.74;0.88;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="0 28 50" to="360 28 50" dur="2.4s" repeatCount="indefinite" />
          <circle cx="40" cy="50" r="3.5" fill="currentColor">
            <animate attributeName="opacity" values="0.60;0.60;0;0;0;0.60;0.60" keyTimes="0;0.28;0.40;0.62;0.74;0.88;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 28 50" to="540 28 50" dur="2.4s" repeatCount="indefinite" />
          <circle cx="40" cy="50" r="3" fill="currentColor">
            <animate attributeName="opacity" values="0.42;0.42;0;0;0;0.42;0.42" keyTimes="0;0.28;0.40;0.62;0.74;0.88;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <circle cx="28" cy="50" r="6" fill="currentColor">
          <animate attributeName="cx"      values="28;28;50;50;50;28;28"      keyTimes="0;0.34;0.46;0.60;0.72;0.86;1" dur="8s" repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0.4 0 0.2 1;0 0 0 0" />
          <animate attributeName="opacity" values="0.55;0.55;0;0;0;0.55;0.55" keyTimes="0;0.34;0.46;0.60;0.72;0.86;1" dur="8s" repeatCount="indefinite" />
        </circle>
        {/* System B — center (72,50), ring r=12 */}
        <circle cx="72" cy="50" r="12" fill="none" stroke="currentColor" strokeWidth="0.4">
          <animate attributeName="opacity" values="0.22;0.22;0;0;0;0.22;0.22" keyTimes="0;0.28;0.40;0.62;0.74;0.88;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="60 72 50" to="420 72 50" dur="2s" repeatCount="indefinite" />
          <circle cx="84" cy="50" r="3.5" fill="currentColor">
            <animate attributeName="opacity" values="0.60;0.60;0;0;0;0.60;0.60" keyTimes="0;0.28;0.40;0.62;0.74;0.88;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="240 72 50" to="600 72 50" dur="2s" repeatCount="indefinite" />
          <circle cx="84" cy="50" r="3" fill="currentColor">
            <animate attributeName="opacity" values="0.42;0.42;0;0;0;0.42;0.42" keyTimes="0;0.28;0.40;0.62;0.74;0.88;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <circle cx="72" cy="50" r="6" fill="currentColor">
          <animate attributeName="cx"      values="72;72;50;50;50;72;72"      keyTimes="0;0.34;0.46;0.60;0.72;0.86;1" dur="8s" repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0.4 0 0.2 1;0 0 0 0" />
          <animate attributeName="opacity" values="0.55;0.55;0;0;0;0.55;0.55" keyTimes="0;0.34;0.46;0.60;0.72;0.86;1" dur="8s" repeatCount="indefinite" />
        </circle>
        {/* Merged state — center (50,50), 2 rings */}
        <circle cx="50" cy="50" r="0" fill="none" stroke="currentColor" strokeWidth="1.5">
          <animate attributeName="r"       values="0;0;8;38;38;0;0" keyTimes="0;0.44;0.46;0.57;0.59;0.62;1" dur="8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.5;0;0;0;0"  keyTimes="0;0.44;0.46;0.57;0.59;0.62;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="0" fill="currentColor">
          <animate attributeName="r"       values="0;0;8;8;8;0;0"       keyTimes="0;0.44;0.50;0.60;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.7;0.7;0.7;0;0" keyTimes="0;0.44;0.50;0.60;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4">
          <animate attributeName="opacity" values="0;0;0;0.25;0.25;0;0" keyTimes="0;0.44;0.52;0.56;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3">
          <animate attributeName="opacity" values="0;0;0;0.18;0.18;0;0" keyTimes="0;0.44;0.54;0.58;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" />
          <circle cx="72" cy="50" r="4" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.65;0.65;0;0" keyTimes="0;0.44;0.52;0.56;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" />
          <circle cx="72" cy="50" r="3.5" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.50;0.50;0;0" keyTimes="0;0.44;0.52;0.56;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="90 50 50" to="-270 50 50" dur="5s" repeatCount="indefinite" />
          <circle cx="86" cy="50" r="3.5" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.55;0.55;0;0" keyTimes="0;0.44;0.54;0.58;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="270 50 50" to="-90 50 50" dur="5s" repeatCount="indefinite" />
          <circle cx="86" cy="50" r="3" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.40;0.40;0;0" keyTimes="0;0.44;0.54;0.58;0.70;0.78;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
      </svg>
    </div>
  );
}

function OverrideLoadingOverlay(): ReactElement {
  return (
    <div className={styles.modalLoadingOverlay}>
      <svg className={styles.modalLoadingSpinner} viewBox="0 0 100 100" overflow="visible" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4" opacity="0.25" />
        <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3" opacity="0.18" />
        <circle cx="50" cy="50" r="7" fill="currentColor" opacity="0.55" />
        <g>
          <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
          <circle cx="72" cy="50" r="5" fill="currentColor" opacity="0.65" />
          <circle cy="50" fill="currentColor">
            <animate attributeName="cx"      values="106;106;72;72;106;106" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="6s"   begin="0s"   repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
            <animate attributeName="r"       values="3.5;3.5;5;3.5;3.5;3.5" keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="6s"   begin="0s"   repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0.82;0.88;0;0;0"      keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="6s"   begin="0s"   repeatCount="indefinite" />
          </circle>
          <circle cx="72" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
            <animate attributeName="r"       values="0;0;5;16;16;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="6s"   begin="0s"   repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0;0.7;0;0;0"  keyTimes="0;0.48;0.5;0.62;0.66;1" dur="6s"   begin="0s"   repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7" opacity="0.3" />
          <circle cx="72" cy="50" r="4" fill="currentColor" opacity="0.5" />
          <circle cy="50" fill="currentColor">
            <animate attributeName="cx"      values="106;106;72;72;106;106" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="7.5s" begin="2.4s" repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
            <animate attributeName="r"       values="3;3;4.5;3;3;3"         keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="7.5s" begin="2.4s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0.75;0.82;0;0;0"     keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="7.5s" begin="2.4s" repeatCount="indefinite" />
          </circle>
          <circle cx="72" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
            <animate attributeName="r"       values="0;0;4;14;14;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="7.5s" begin="2.4s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0;0.65;0;0;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="7.5s" begin="2.4s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="60 50 50" to="-300 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
          <circle cx="86" cy="50" r="4" fill="currentColor" opacity="0.55" />
          <circle cy="50" fill="currentColor">
            <animate attributeName="cx"      values="108;108;86;86;108;108" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="8s"   begin="1s"   repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
            <animate attributeName="r"       values="3.5;3.5;5;3.5;3.5;3.5" keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="8s"   begin="1s"   repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0.78;0.85;0;0;0"     keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="8s"   begin="1s"   repeatCount="indefinite" />
          </circle>
          <circle cx="86" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
            <animate attributeName="r"       values="0;0;5;16;16;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="8s"   begin="1s"   repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0;0.65;0;0;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="8s"   begin="1s"   repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="-180 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
          <circle cx="86" cy="50" r="3.5" fill="currentColor" opacity="0.45" />
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="300 50 50" to="-60 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5" opacity="0.22" />
          <circle cx="86" cy="50" r="3" fill="currentColor" opacity="0.4" />
          <circle cy="50" fill="currentColor">
            <animate attributeName="cx"      values="108;108;86;86;108;108" keyTimes="0;0.05;0.5;0.72;0.78;1" dur="9s"   begin="3.8s" repeatCount="indefinite" calcMode="spline" keySplines="0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0;0 0 0 0" />
            <animate attributeName="r"       values="3;3;4.5;3;3;3"         keyTimes="0;0.05;0.52;0.6;0.72;1"  dur="9s"   begin="3.8s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0.7;0.8;0;0;0"       keyTimes="0;0.07;0.5;0.72;0.78;1"  dur="9s"   begin="3.8s" repeatCount="indefinite" />
          </circle>
          <circle cx="86" cy="50" fill="none" stroke="currentColor" strokeWidth="1">
            <animate attributeName="r"       values="0;0;4;14;14;0" keyTimes="0;0.48;0.5;0.62;0.66;1" dur="9s"   begin="3.8s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0;0;0.6;0;0;0"  keyTimes="0;0.48;0.5;0.62;0.66;1" dur="9s"   begin="3.8s" repeatCount="indefinite" />
          </circle>
        </g>
      </svg>
    </div>
  );
}

function TabEmptyState({ message }: { message: string }): ReactElement {
  return (
    <div className={styles.tabEmptyState}>
      <svg className={styles.tabEmptyIcon} viewBox="0 0 80 72" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        {/* cloud body */}
        <path
          d="M18 38 Q16 38 14 36 Q10 34 10 30 Q10 24 16 23 Q17 17 24 16 Q31 15 34 20 Q36 15 42 15 Q49 15 51 21 Q57 22 58 28 Q59 34 55 37 Q53 38 50 38 Z"
          fill="currentColor" opacity="0.12"
        />
        <path
          d="M18 38 Q16 38 14 36 Q10 34 10 30 Q10 24 16 23 Q17 17 24 16 Q31 15 34 20 Q36 15 42 15 Q49 15 51 21 Q57 22 58 28 Q59 34 55 37 Q53 38 50 38 Z"
          stroke="currentColor" strokeWidth="1.4" opacity="0.6"
        />
        {/* rain lines — 5 drops, varied lengths and offsets */}
        <line x1="21" y1="43" x2="18" y2="52" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.35"/>
        <line x1="30" y1="43" x2="27" y2="54" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.55"/>
        <line x1="39" y1="43" x2="37" y2="56" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.65"/>
        <line x1="48" y1="43" x2="46" y2="54" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.55"/>
        <line x1="57" y1="43" x2="55" y2="52" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" opacity="0.35"/>
        {/* puddle — thin oval at bottom */}
        <ellipse cx="38" cy="65" rx="18" ry="3.5" fill="currentColor" opacity="0.1"/>
        <ellipse cx="38" cy="65" rx="18" ry="3.5" stroke="currentColor" strokeWidth="1" opacity="0.25"/>
        {/* two tiny floating dots near cloud — like mist */}
        <circle cx="6" cy="28" r="1.8" fill="currentColor" opacity="0.2"/>
        <circle cx="68" cy="24" r="1.4" fill="currentColor" opacity="0.15"/>
        <circle cx="64" cy="34" r="1" fill="currentColor" opacity="0.12"/>
      </svg>
      <p className={styles.tabEmptyMsg}>{message}</p>
    </div>
  );
}

function SkeletonRows(): ReactElement {
  return (
    <>
      {SKEL_N.map((i) => (
        <div key={i} className={styles.tabSkelItem}>
          <div className={styles.tabSkelStack}>
            <span className={styles.tabSkelLine} style={{ width: `${55 + (i % 3) * 12}%` }} />
            <span className={styles.tabSkelLine} style={{ width: `${35 + (i % 4) * 10}%`, height: 10 }} />
          </div>
          <span className={styles.tabSkelBadge} style={{ width: 52 }} />
        </div>
      ))}
    </>
  );
}

function SkeletonConnections(): ReactElement {
  return (
    <>
      {SKEL_N.map((i) => (
        <div key={i} className={styles.tabSkelItem}>
          <div className={styles.tabSkelAvatar} />
          <div className={styles.tabSkelStack}>
            <span className={styles.tabSkelLine} style={{ width: `${50 + (i % 3) * 15}%` }} />
            <span className={styles.tabSkelLine} style={{ width: `${65 + (i % 3) * 8}%`, height: 10 }} />
          </div>
        </div>
      ))}
    </>
  );
}

function SkeletonAudit(): ReactElement {
  return (
    <>
      {SKEL_N.map((i) => (
        <div key={i} className={styles.tabSkelItem} style={{ alignItems: "flex-start" }}>
          <div className={styles.tabSkelDot} />
          <div className={styles.tabSkelStack}>
            <span className={styles.tabSkelLine} style={{ width: `${45 + (i % 3) * 12}%` }} />
            <span className={styles.tabSkelLine} style={{ width: `${30 + (i % 4) * 8}%`, height: 10 }} />
          </div>
        </div>
      ))}
    </>
  );
}

function SkeletonMatches(): ReactElement {
  return (
    <>
      {SKEL_N.map((i) => (
        <div key={i} className={styles.tabSkelItem}>
          <span className={styles.tabSkelBadge} style={{ width: 64 }} />
          <span className={styles.tabSkelLine} style={{ width: 72 }} />
          <span className={styles.tabSkelLine} style={{ flex: 1 }} />
          <span className={styles.tabSkelLine} style={{ width: 80 }} />
          <span className={styles.tabSkelLine} style={{ width: 90 }} />
        </div>
      ))}
    </>
  );
}

function TabSkelShell({ title, children }: { title: string; children: ReactElement }): ReactElement {
  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>{title}</span>
      </div>
      {children}
    </section>
  );
}

function MatchesTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const [expandedMatch, setExpandedMatch] = useState<string | null>(null);
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonMatchDecision>(`/bff/persons/${encodeURIComponent(personId)}/matches`);
  const matches = rows ?? [];

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="Match decisions"><SkeletonMatches /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Match decisions</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? matches.length} {(total ?? matches.length) === 1 ? "decision" : "decisions"}</span>
      </div>
      {matches.length === 0 && <TabEmptyState message="No match decisions on record." />}
      {matches.length > 0 && (
        <div className={styles.matchList}>
          <div className={styles.matchHeaderRow}>
            <span>Decision</span>
            <span>Engine</span>
            <span>Counterpart</span>
            <span>Confidence</span>
            <span>Created</span>
          </div>
          {matches.map((match) => {
          const isOpen = expandedMatch === match.match_decision_id;
          const otherPersonId = match.left_person_id === personId ? match.right_person_id : match.left_person_id;
          return (
            <div key={match.match_decision_id} className={`${styles.matchRow} ${isOpen ? styles.matchRowOpen : ""}`}>
              <button
                type="button"
                className={styles.matchRowButton}
                onClick={() => setExpandedMatch(isOpen ? null : match.match_decision_id)}
                aria-expanded={isOpen}
              >
                <span className={styles.matchDecisionCell}>
                  <span className={[styles.matchDecisionBadge, decisionBadgeClass(match.decision)].filter(Boolean).join(" ")}>
                    {titleCase(match.decision)}
                  </span>
                  <span className={`${styles.matchId} ${styles.mono}`}>{match.match_decision_id}</span>
                </span>
                <span className={styles.matchEngineCell}>
                  <span>{titleCase(match.engine_type)}</span>
                  <span className={`${styles.matchSubText} ${styles.mono}`}>{match.engine_version}</span>
                </span>
                <span className={`${styles.matchPersonCell} ${styles.mono}`}>{otherPersonId ?? "—"}</span>
                <span className={styles.matchConfidenceCell}>
                  <span className={styles.matchConfidenceValue}>{(match.confidence * 100).toFixed(1)}%</span>
                  <span className={styles.matchConfidenceTrack}>
                    <span className={styles.matchConfidenceFill} style={{ width: `${Math.max(0, Math.min(100, match.confidence * 100))}%` }} />
                  </span>
                </span>
                <span className={styles.matchCreatedCell}>{fmtDateTime(match.created_at)}</span>
                <svg className={`${styles.matchChevron} ${isOpen ? styles.matchChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </button>
              {isOpen && (
                <div className={styles.matchDetail}>
                  <div className={styles.matchMetaGrid}>
                    {[
                      { label: "Policy", value: match.policy_version },
                      { label: "Left", value: match.left_person_id },
                      { label: "Right", value: match.right_person_id },
                      { label: "Conflicts", value: match.blocking_conflicts.length ? match.blocking_conflicts.join(", ") : "None" },
                    ].map(({ label, value }) => (
                      <div key={label} className={styles.matchMetaItem}>
                        <span className={styles.matchMetaLabel}>{label}</span>
                        <span className={`${styles.matchMetaValue} ${styles.mono}`}>{value ?? "—"}</span>
                      </div>
                    ))}
                  </div>
                  <div className={styles.matchReasonsBlock}>
                    <span className={styles.matchMetaLabel}>Reasons</span>
                    <div className={styles.matchReasonList}>
                      {match.reasons.length > 0 ? match.reasons.map((reason) => (
                        <span key={reason} className={styles.matchReasonChip}>{reason}</span>
                      )) : <span className={styles.matchEmptyText}>No reasons recorded.</span>}
                    </div>
                  </div>
                </div>
              )}
            </div>
          );
          })}
        </div>
      )}
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}

function IdentifiersTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonIdentifier>(`/bff/persons/${encodeURIComponent(personId)}/identifiers`);
  const identifiers = rows ?? [];

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="IDs"><SkeletonRows /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>IDs</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? identifiers.length} {(total ?? identifiers.length) === 1 ? "identifier" : "identifiers"}</span>
      </div>
      {identifiers.length === 0 && <TabEmptyState message="No identifiers on record." />}
      <div className={styles.idList}>
        {identifiers.map((id: PersonIdentifier, index: number) => (
          <div key={`${id.identifier_type}-${id.normalized_value}-${index}`} className={styles.idRow}>
            <div className={styles.idIconWrap}>
              <IdTypeIcon type={id.identifier_type} />
            </div>
            <div className={styles.idBody}>
              <span className={styles.idValue}>{id.normalized_value}</span>
              <div className={styles.connMeta}>
                <span>{titleCase(id.identifier_type)}</span>
                {id.source_system_key && (
                  <>
                    <span className={styles.connMetaSep}>·</span>
                    <span>{id.source_system_key}</span>
                  </>
                )}
                {id.source_record_ids && id.source_record_ids.length > 0 && (
                  <>
                    <span className={styles.connMetaSep}>·</span>
                    <span className={styles.mono}>{id.source_record_ids.join(", ")}</span>
                  </>
                )}
                {id.last_confirmed_at && (
                  <>
                    <span className={styles.connMetaSep}>·</span>
                    <span>{fmtDate(id.last_confirmed_at)}</span>
                  </>
                )}
              </div>
            </div>
            <div className={styles.idBadges}>
              {id.is_verified && <span className={styles.idBadgeVerified}>Verified</span>}
              <span className={id.is_active ? styles.idBadgeActive : styles.idBadgeInactive}>
                {id.is_active ? "Active" : "Inactive"}
              </span>
            </div>
          </div>
        ))}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}

function SourceRecordDetail({ record }: { record: PersonSourceRecord }): ReactElement {
  const payload = record.normalized_payload;

  return (
    <div className={styles.srcDetail}>
      <div className={styles.srcDetailLayout}>

        {/* ── Left: identity + timeline ── */}
        <div className={styles.srcDetailLeft}>
          <div className={styles.srcDetailIdentity}>
            <span className={styles.srcDetailSystem}>{record.source_system}</span>
            {record.entity_display_name && <span className={styles.srcDetailEntity}>{record.entity_display_name}</span>}
            <div className={styles.srcDetailBadgeRow}>
              <span className={styles.srcTypeBadge}>{record.record_type}</span>
              <span className={record.link_status === "linked" ? styles.idBadgeActive : styles.idBadgeInactive}>
                {record.link_status}
              </span>
            </div>
          </div>

          <div className={styles.srcTimeline}>
            {[
              { label: "Observed", value: fmtDateTime(record.observed_at) },
              { label: "Ingested", value: fmtDateTime(record.ingested_at) },
              ...(record.extraction_method ? [{ label: "Method", value: record.extraction_method }] : []),
              ...(record.extraction_confidence != null ? [{ label: "Confidence", value: `${Math.round(record.extraction_confidence * 100)}%` }] : []),
            ].map(({ label, value }, i, arr) => (
              <div key={label} className={`${styles.srcTimelineItem} ${i === arr.length - 1 ? styles.srcTimelineLast : ""}`}>
                <div className={styles.srcTimelineDot} />
                <div>
                  <div className={styles.srcTimelineLabel}>{label}</div>
                  <div className={styles.srcTimelineValue}>{value}</div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* ── Right: extracted data ── */}
        <div className={styles.srcDetailRight}>
          {payload?.identifiers && payload.identifiers.length > 0 && (
            <div className={styles.srcDataBlock}>
              <div className={styles.srcDataBlockTitle}>Identifiers</div>
              <div className={styles.srcIdentList}>
                {payload.identifiers.map((id, i) => (
                  <div key={i} className={styles.srcIdentRow}>
                    <span className={id.is_verified ? styles.srcIdentDotVerified : styles.srcIdentDot} />
                    <span className={styles.srcIdentType}>{titleCase(id.identifier_type ?? "")}</span>
                    <span className={styles.srcIdentValue}>{id.normalized_value}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {payload?.address && (
            <div className={styles.srcDataBlock}>
              <div className={styles.srcDataBlockTitle}>Address</div>
              <div className={styles.srcAddressFull}>{payload.address.normalized_full ?? "—"}</div>
              <div className={styles.srcAddressMeta}>
                {[payload.address.city, payload.address.postal_code, payload.address.country_code, payload.address.quality_flag]
                  .filter(Boolean).join(" · ")}
              </div>
            </div>
          )}

          {payload?.attributes && payload.attributes.length > 0 && (
            <div className={styles.srcDataBlock}>
              <div className={styles.srcDataBlockTitle}>Attributes</div>
              <div className={styles.srcAttrGrid}>
                {payload.attributes.map((attr, i) => (
                  <div key={i} className={styles.srcAttrItem}>
                    <span className={styles.srcAttrLabel}>{titleCase(attr.attribute_name ?? "")}</span>
                    <span className={styles.srcAttrValue}>{attr.attribute_value ?? "—"}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {payload?.summary && (
            <div className={styles.srcDataBlock}>
              <div className={styles.srcDataBlockTitle}>Summary</div>
              <div className={styles.srcSummaryText}>{payload.summary}</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function SourceRecordsTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const [expandedPk, setExpandedPk] = useState<string | null>(null);
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonSourceRecord>(`/bff/persons/${encodeURIComponent(personId)}/source-records`);
  const sourceRecords = rows ?? [];

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="Sources"><SkeletonRows /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Sources</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? sourceRecords.length} {(total ?? sourceRecords.length) === 1 ? "record" : "records"}</span>
      </div>
      {sourceRecords.length === 0 && <TabEmptyState message="No source records linked." />}
      <div className={styles.idList}>
        {sourceRecords.map((record) => {
          const isOpen = expandedPk === record.source_record_pk;
          return (
            <div key={record.source_record_pk} className={`${styles.srcRow} ${isOpen ? styles.srcRowOpen : ""}`}>
              <div
                className={styles.srcMain}
                onClick={() => setExpandedPk(isOpen ? null : record.source_record_pk)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setExpandedPk(isOpen ? null : record.source_record_pk)}
              >
                <div className={styles.idBody}>
                  <span className={styles.idValue}>{record.source_record_id}</span>
                  <div className={styles.connMeta}>
                    <span>{record.source_system}</span>
                    {record.entity_display_name && <><span className={styles.connMetaSep}>·</span><span>{record.entity_display_name}</span></>}
                    <span className={styles.connMetaSep}>·</span>
                    <span>Observed {fmtDateTime(record.observed_at)}</span>
                  </div>
                </div>
                <div className={styles.idBadges}>
                  <span className={styles.srcTypeBadge}>{record.record_type}</span>
                  <span className={record.link_status === "linked" ? styles.idBadgeActive : styles.idBadgeInactive}>{record.link_status}</span>
                  <svg className={`${styles.srcChevron} ${isOpen ? styles.srcChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </div>
              </div>
              {isOpen && <SourceRecordDetail record={record} />}
            </div>
          );
        })}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}

function SalesTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<SalesOrder>(`/bff/persons/${encodeURIComponent(personId)}/sales`);
  const sales = rows ?? [];
  const pageRevenue = sales.reduce((sum, o) => sum + (o.total_amount ?? 0), 0);
  const currency = sales[0]?.currency ?? "SGD";

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="Sales history"><SkeletonRows /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Sales history</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? sales.length} orders</span>
        {sales.length > 0 && <><span className={styles.connHeaderDot}>·</span><span className={styles.connHeaderCount}>{fmtCurrency(pageRevenue, currency)} this page</span></>}
      </div>
      {sales.length === 0 && <TabEmptyState message="No orders on record." />}
      <div className={styles.idList}>
        {sales.map((order, index) => {
          const key = order.order_no ?? order.source_order_id ?? `order-${index}`;
          const isOpen = expandedKey === key;
          const source = order.entity_name ?? order.source_system;
          return (
            <div key={key} className={`${styles.srcRow} ${isOpen ? styles.srcRowOpen : ""}`}>
              <div
                className={styles.srcMain}
                onClick={() => setExpandedKey(isOpen ? null : key)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setExpandedKey(isOpen ? null : key)}
              >
                <div className={styles.idBody}>
                  <span className={styles.idValue}>{key}</span>
                  <div className={styles.connMeta}>
                    <span>Ordered {fmtDate(order.order_date)}</span>
                    {order.release_date && (
                      <><span className={styles.connMetaSep}>·</span><span>Released {fmtDate(order.release_date)}</span></>
                    )}
                    {source && (
                      <><span className={styles.connMetaSep}>·</span><span>{source}</span></>
                    )}
                  </div>
                </div>
                <div className={styles.idBadges}>
                  <span className={styles.salesTotal}>{fmtCurrency(order.total_amount, order.currency)}</span>
                  <span className={styles.salesItemCount}>{order.line_items.length} {order.line_items.length === 1 ? "item" : "items"}</span>
                  <svg className={`${styles.srcChevron} ${isOpen ? styles.srcChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </div>
              </div>
              {isOpen && (
                <div className={styles.salesDetail}>
                  <div className={styles.salesLineTable}>
                    <div className={`${styles.salesLineRow} ${styles.salesLineHeader}`}>
                      <span>Line #</span>
                      <span>Product</span>
                      <span>Category</span>
                      <span>SKU</span>
                      <span className={styles.salesCellRight}>Qty</span>
                      <span className={styles.salesCellRight}>Unit price</span>
                      <span className={styles.salesCellRight}>Subtotal</span>
                    </div>
                    {order.line_items.map((item, i) => (
                      <div key={i} className={styles.salesLineRow}>
                        <span className={styles.salesCellMuted}>{item.line_no ?? i + 1}</span>
                        <span className={styles.salesCellPrimary}>{item.product?.display_name ?? "—"}</span>
                        <span className={styles.salesCellMuted}>{item.product?.category ?? "—"}</span>
                        <span className={styles.salesCellMono}>{item.product?.sku ?? "—"}</span>
                        <span className={`${styles.salesCellMuted} ${styles.salesCellRight}`}>{item.quantity ?? "—"}</span>
                        <span className={`${styles.salesCellMuted} ${styles.salesCellRight}`}>{item.unit_price != null ? item.unit_price.toFixed(2) : "—"}</span>
                        <span className={`${styles.salesCellPrimary} ${styles.salesCellRight}`}>{item.subtotal != null ? fmtCurrency(item.subtotal, order.currency) : "—"}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}

function BankruptcyTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const [expandedCase, setExpandedCase] = useState<string | null>(null);
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonBankruptcyCase>(`/bff/persons/${encodeURIComponent(personId)}/bankruptcy-cases`);
  const cases = rows ?? [];

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="Bankruptcy"><SkeletonRows /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Bankruptcy</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? cases.length} {(total ?? cases.length) === 1 ? "case" : "cases"}</span>
      </div>
      {cases.length === 0 && <TabEmptyState message="No bankruptcy cases on record." />}
      <div className={styles.idList}>
        {cases.map((bkCase) => {
          const key = bkCase.bankruptcy_case_id;
          const isOpen = expandedCase === key;
          const displayName = bkCase.case_number ?? bkCase.source_case_id;
          return (
            <div key={key} className={`${styles.srcRow} ${isOpen ? styles.srcRowOpen : ""}`}>
              <div
                className={styles.srcMain}
                onClick={() => setExpandedCase(isOpen ? null : key)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => e.key === "Enter" && setExpandedCase(isOpen ? null : key)}
              >
                <div className={styles.idBody}>
                  <span className={styles.idValue}>{displayName}</span>
                  <div className={styles.connMeta}>
                    <span>{bkCase.source_system_key}</span>
                    {bkCase.event_type && (
                      <><span className={styles.connMetaSep}>·</span><span>{titleCase(bkCase.event_type)}</span></>
                    )}
                    {bkCase.event_date && (
                      <><span className={styles.connMetaSep}>·</span><span>{fmtDate(bkCase.event_date)}</span></>
                    )}
                  </div>
                </div>
                <div className={styles.idBadges}>
                  {bkCase.document_type && (
                    <span className={styles.srcTypeBadge}>{titleCase(bkCase.document_type)}</span>
                  )}
                  <svg className={`${styles.srcChevron} ${isOpen ? styles.srcChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </div>
              </div>
              {isOpen && (
                <div className={styles.bkDetail}>
                  <div className={styles.bkMeta}>
                    {[
                      { label: "Case number", value: bkCase.case_number },
                      { label: "Source case ID", value: bkCase.source_case_id },
                      { label: "Source system", value: bkCase.source_system_key },
                      { label: "Document type", value: bkCase.document_type ? titleCase(bkCase.document_type) : null },
                      { label: "Document date", value: bkCase.document_date ? fmtDate(bkCase.document_date) : null },
                      { label: "Event type", value: bkCase.event_type ? titleCase(bkCase.event_type) : null },
                      { label: "Event date", value: bkCase.event_date ? fmtDate(bkCase.event_date) : null },
                      { label: "Trustee", value: bkCase.trustee_name },
                      { label: "Trustee firm", value: bkCase.trustee_firm },
                      { label: "First seen", value: bkCase.first_seen_at ? fmtDate(bkCase.first_seen_at) : null },
                      { label: "Last seen", value: bkCase.last_seen_at ? fmtDate(bkCase.last_seen_at) : null },
                    ]
                      .filter(({ value }) => value != null)
                      .map(({ label, value }) => (
                        <div key={label} className={styles.srcDetailField}>
                          <div className={styles.srcDetailLabel}>{label}</div>
                          <div className={styles.srcDetailValue}>{value}</div>
                        </div>
                      ))}
                    {bkCase.source_url && (
                      <div className={styles.srcDetailField}>
                        <div className={styles.srcDetailLabel}>Source URL</div>
                        <div className={styles.srcDetailValue}>
                          <a
                            href={bkCase.source_url.startsWith("http") ? bkCase.source_url : "#"}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {bkCase.source_url}
                          </a>
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}

function AuditTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonAuditEvent>(`/bff/persons/${encodeURIComponent(personId)}/audit`);
  const audit = rows ?? [];

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="Audit trail"><SkeletonAudit /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Audit trail</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? audit.length} {(total ?? audit.length) === 1 ? "event" : "events"}</span>
      </div>
      {audit.length === 0 && <TabEmptyState message="No audit events on record." />}
      <div className={styles.auditList}>
        {audit.map((event, i) => (
          <div key={event.merge_event_id} className={styles.auditItem}>
            <div className={styles.auditRail}>
              <div className={styles.auditDot} />
              {i < audit.length - 1 && <div className={styles.auditLine} />}
            </div>
            <div className={styles.auditBody}>
              <div className={styles.auditTop}>
                <span className={styles.auditEventType}>{event.event_type}</span>
                <span className={styles.auditTime}>{fmtDateTime(event.created_at)}</span>
              </div>
              <div className={styles.auditActor}>
                {event.actor_type}:{event.actor_id}
              </div>
              {event.reason && <div className={styles.auditReason}>{event.reason}</div>}
            </div>
          </div>
        ))}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}

function PhoneIcon(): ReactElement {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style={{ flexShrink: 0 }}>
      <path d="M6.6 10.8c1.4 2.8 3.8 5.1 6.6 6.6l2.2-2.2c.3-.3.7-.4 1-.2 1.1.4 2.3.6 3.6.6.6 0 1 .4 1 1V20c0 .6-.4 1-1 1-9.4 0-17-7.6-17-17 0-.6.4-1 1-1h3.5c.6 0 1 .4 1 1 0 1.3.2 2.5.6 3.6.1.3 0 .7-.2 1L6.6 10.8z" />
    </svg>
  );
}

function EmailIcon(): ReactElement {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style={{ flexShrink: 0 }}>
      <path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z" />
    </svg>
  );
}

function LocationIcon(): ReactElement {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style={{ flexShrink: 0 }}>
      <path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7zm0 9.5c-1.38 0-2.5-1.12-2.5-2.5s1.12-2.5 2.5-2.5 2.5 1.12 2.5 2.5-1.12 2.5-2.5 2.5z" />
    </svg>
  );
}

function KeyIcon(): ReactElement {
  return (
    <svg width="10" height="10" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" style={{ flexShrink: 0 }}>
      <path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z" />
    </svg>
  );
}

function identifierIcon(type: string): ReactElement {
  if (type === "phone") return <PhoneIcon />;
  if (type === "email") return <EmailIcon />;
  return <KeyIcon />;
}

type ConnMetaLineProps = {
  hops: number;
  sharedIdentifiers: Array<{ identifier_type: string; normalized_value: string }>;
  sharedAddresses: Array<{ normalized_full: string | null }>;
};

function ConnMetaLine({ hops, sharedIdentifiers, sharedAddresses }: ConnMetaLineProps): ReactElement {
  const items: ReactElement[] = [
    <span key="hops">{hops} hop{hops !== 1 ? "s" : ""}</span>,
    ...sharedIdentifiers.map((id, i) => (
      <span key={`id-${i}`} className={styles.connMetaWithIcon}>
        {identifierIcon(id.identifier_type)}
        {id.normalized_value}
      </span>
    )),
    ...sharedAddresses.map((addr, i) => (
      <span key={`addr-${i}`} className={styles.connMetaWithIcon}>
        <LocationIcon />
        {addr.normalized_full ?? "shared address"}
      </span>
    )),
  ];

  return (
    <div className={styles.connMeta}>
      {items.map((item, i) => (
        <Fragment key={i}>
          {i > 0 && <span className={styles.connMetaSep}>·</span>}
          {item}
        </Fragment>
      ))}
    </div>
  );
}


function ConnectionsTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonConnection>(`/bff/persons/${encodeURIComponent(personId)}/connections?connection_type=all`);
  const connections = rows ?? [];
  const withRel = connections.filter((c) => c.knows_relationships.length > 0);
  const sharedOnly = connections.filter((c) => c.knows_relationships.length === 0);

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  if (loading) return <TabSkelShell title="Connections"><SkeletonConnections /></TabSkelShell>;
  if (error) return <section className={styles.contentCard}><div className={styles.tabError}>{error}</div></section>;

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Connections</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{total ?? connections.length} {(total ?? connections.length) === 1 ? "profile" : "profiles"}</span>
      </div>
      {connections.length === 0 && <TabEmptyState message="No linked profiles found." />}
      <div className={styles.connSections}>
        {withRel.length > 0 && (
          <div>
            <div className={styles.sharedList}>
              {withRel.map((conn, i) => {
                const color = avatarColor(conn.preferred_full_name ?? "?");
                return (
                  <Link key={`${conn.person_id}-${i}`} href={`/persons/${conn.person_id}`} className={styles.sharedRow}>
                    <div className={styles.sharedAvatar} style={{ background: color }}>
                      {personInitials(conn.preferred_full_name)}
                    </div>
                    <div className={styles.connBody}>
                      <div className={styles.sharedName}>{conn.preferred_full_name ?? conn.person_id}</div>
                      <ConnMetaLine hops={conn.hops} sharedIdentifiers={conn.shared_identifiers} sharedAddresses={conn.shared_addresses} />
                    </div>
                    <div className={styles.relRowTags}>
                      {conn.knows_relationships.map((rel, j) => (
                        <span key={j} className={styles.relBadge}>
                          {rel.relationship_label ?? rel.relationship_category}
                        </span>
                      ))}
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>
        )}

        {sharedOnly.length > 0 && (
          <div>
            <div className={styles.sharedList}>
              {sharedOnly.map((conn, i) => {
                const color = avatarColor(conn.preferred_full_name ?? "?");
                return (
                  <Link key={`${conn.person_id}-${i}`} href={`/persons/${conn.person_id}`} className={styles.sharedRow}>
                    <div className={styles.sharedAvatar} style={{ background: color }}>
                      {personInitials(conn.preferred_full_name)}
                    </div>
                    <div className={styles.connBody}>
                      <div className={styles.sharedName}>{conn.preferred_full_name ?? conn.person_id}</div>
                      <ConnMetaLine hops={conn.hops} sharedIdentifiers={conn.shared_identifiers} sharedAddresses={conn.shared_addresses} />
                    </div>
                  </Link>
                );
              })}
            </div>
          </div>
        )}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </section>
  );
}


function PersonDetailSkeleton(): ReactElement {
  return (
    <div className={styles.page}>
      {/* breadcrumb */}
      <div className={styles.breadcrumbRow}>
        <span className={styles.skel} style={{ width: 200, height: 14 }} />
      </div>

      <div className={styles.detailLayout}>
        {/* ── Sidebar ── */}
        <aside className={styles.sidebar}>
          <section className={styles.sidebarHeroCard}>
            <div className={styles.sidebarHeroTop}>
              {/* avatar ring */}
              <span className={styles.skelRing} style={{ width: 64, height: 64, flexShrink: 0 }} />
              <div className={styles.sidebarHeroIdentity} style={{ gap: 8 }}>
                <span className={styles.skel} style={{ width: "75%", height: 16 }} />
                <span className={styles.skel} style={{ width: "55%", height: 11 }} />
                <span className={styles.skel} style={{ width: "40%", height: 8, marginTop: 4 }} />
              </div>
            </div>
            <div className={styles.sidebarHeroSummaryRows} style={{ marginTop: 12 }}>
              {[80, 60, 70, 90, 65, 75].map((w, i) => (
                <div key={i} className={styles.sidebarHeroSummaryRow}>
                  <span className={styles.skel} style={{ width: 48, height: 11 }} />
                  <span className={styles.skel} style={{ width: w, height: 11 }} />
                </div>
              ))}
            </div>
          </section>
          <section className={styles.sidebarCard} style={{ paddingTop: 10, paddingBottom: 10 }}>
            <span className={styles.skel} style={{ width: 60, height: 11, marginBottom: 10 }} />
            <span className={styles.skel} style={{ width: "90%", height: 11, marginBottom: 6 }} />
            <span className={styles.skel} style={{ width: "70%", height: 11 }} />
          </section>
        </aside>

        {/* ── Right rail ── */}
        <div className={styles.mainColumn}>
          {/* summary strip */}
          <section className={styles.summaryStrip}>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className={styles.summaryCard}>
                <span className={styles.skel} style={{ width: 70, height: 10, marginBottom: 8 }} />
                <span className={styles.skel} style={{ width: 52, height: 20, marginBottom: 6 }} />
                <span className={styles.skel} style={{ width: 80, height: 10 }} />
              </div>
            ))}
          </section>

          {/* tabs row */}
          <div className={styles.rightTabsInline}>
            <div className={styles.tabs}>
              {[72, 56, 64, 36, 52, 44, 72, 40, 44].map((w, i) => (
                <span key={i} className={styles.skel} style={{ width: w, height: 28, borderRadius: 6 }} />
              ))}
            </div>
          </div>

          {/* content area */}
          <div className={styles.tabPanelScroll}>
            <section className={styles.contentCard}>
              <div className={styles.connHeader}>
                <span className={styles.skel} style={{ width: 60, height: 13 }} />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 14, padding: "4px 0" }}>
                {[0, 1, 2, 3, 4].map((i) => (
                  <div key={i} style={{ display: "flex", flexDirection: "column", gap: 6, paddingBottom: 14, borderBottom: "1px solid var(--border)" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <span className={styles.skel} style={{ width: [120, 90, 110, 80, 100][i], height: 13 }} />
                      <span className={styles.skel} style={{ width: 60, height: 11 }} />
                    </div>
                    <span className={styles.skel} style={{ width: [200, 160, 180, 140, 170][i], height: 11 }} />
                  </div>
                ))}
              </div>
            </section>
          </div>
        </div>
      </div>
    </div>
  );
}

const EMPTY_DETAIL: DetailData = {
  identifiers: [],
  sourceRecords: [],
  sales: [],
  audit: [],
};

const VALID_TABS = new Set<Tab>(["timeline", "matches", "connections", "identifier", "source", "sales", "bankruptcy", "audit", "graph"]);

function parseTabParam(value: string | null): Tab {
  if (value !== null && VALID_TABS.has(value as Tab)) return value as Tab;
  return "timeline";
}

export default function PersonDetailPage({ params }: { params: Promise<{ personId: string }> }): ReactElement {
  const { personId } = use(params);
  const searchParams = useSearchParams();

  const [person, setPerson] = useState<Person | null>(null);
  const [detailData, setDetailData] = useState<DetailData>(EMPTY_DETAIL);
  const [tabTotals, setTabTotals] = useState<Partial<Record<Tab, number>>>({});
  const [loading, setLoading] = useState(true);
  const [notFoundFlag, setNotFoundFlag] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>(() => parseTabParam(searchParams.get("tab")));
  const [graphOpen, setGraphOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [shareExpiry, setShareExpiry] = useState<string | null>(null);
  const [shareCopied, setShareCopied] = useState(false);

  async function handleShare(): Promise<void> {
    if (shareUrl) { setShareOpen(true); return; }
    setShareLoading(true);
    try {
      const res = await bffFetchEnvelope<PublicLink>(`/bff/persons/${encodeURIComponent(personId)}/public-link`, { method: "POST" });
      const url = `${window.location.origin}/public/persons/${res.data.token}`;
      setShareUrl(url);
      setShareExpiry(res.data.expires_at);
      setShareOpen(true);
    } finally {
      setShareLoading(false);
    }
  }

  function handleCopy(): void {
    if (!shareUrl) return;
    void navigator.clipboard.writeText(shareUrl).then(() => {
      setShareCopied(true);
      setTimeout(() => setShareCopied(false), 2000);
    });
  }

  function closeShare(): void {
    setShareOpen(false);
  }

  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeQuery, setMergeQuery] = useState("");
  const [mergeResults, setMergeResults] = useState<Person[]>([]);
  const [mergeSearching, setMergeSearching] = useState(false);
  const [mergeTarget, setMergeTarget] = useState<Person | null>(null);
  const [mergeReason, setMergeReason] = useState("");
  const [mergeSubmitting, setMergeSubmitting] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [mergeSuccess, setMergeSuccess] = useState(false);

  useEffect(() => {
    if (!mergeOpen) return;
    if (mergeQuery.trim().length < 2) { setMergeResults([]); return; }
    let cancelled = false;
    setMergeSearching(true);
    void bffFetchEnvelope<Person[]>(`/bff/persons/search?q=${encodeURIComponent(mergeQuery)}&limit=8`)
      .then((res) => { if (!cancelled) setMergeResults(res.data.filter((p) => p.person_id !== personId)); })
      .catch(() => { if (!cancelled) setMergeResults([]); })
      .finally(() => { if (!cancelled) setMergeSearching(false); });
    return () => { cancelled = true; };
  }, [mergeQuery, mergeOpen, personId]);

  async function handleMergeSubmit(): Promise<void> {
    if (!mergeTarget || !mergeReason.trim()) return;
    setMergeSubmitting(true);
    setMergeError(null);
    try {
      const body: ManualMergeRequestBody = {
        from_person_id: personId,
        to_person_id: mergeTarget.person_id,
        reason: mergeReason.trim(),
        recompute_golden_profile: true,
      };
      const res = await bffFetchEnvelope<ManualMergeResponseBody>("/bff/persons/manual-merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.data.status === "merged") {
        setMergeSuccess(true);
        setTimeout(() => window.location.replace(`/persons/${mergeTarget.person_id}`), 1500);
      }
    } catch (e) {
      setMergeError(e instanceof BffError ? e.message : "Failed to merge.");
    } finally {
      setMergeSubmitting(false);
    }
  }

  function closeMerge(): void {
    setMergeOpen(false);
    setMergeQuery("");
    setMergeResults([]);
    setMergeTarget(null);
    setMergeReason("");
    setMergeError(null);
    setMergeSuccess(false);
  }

  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideField, setOverrideField] = useState<string>("full_name");
  const [overrideSrPk, setOverrideSrPk] = useState<string>("");
  const [overrideReason, setOverrideReason] = useState("");
  const [overrideSubmitting, setOverrideSubmitting] = useState(false);
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const [overrideSuccess, setOverrideSuccess] = useState(false);

  async function handleOverrideSubmit(): Promise<void> {
    if (!overrideSrPk || !overrideReason.trim()) return;
    setOverrideSubmitting(true);
    setOverrideError(null);
    try {
      const body: SurvivorshipOverrideRequestBody = {
        attribute_name: overrideField,
        selected_source_record_pk: overrideSrPk,
        reason: overrideReason.trim(),
      };
      await bffFetchEnvelope(`/bff/persons/${encodeURIComponent(personId)}/survivorship-overrides`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setOverrideSuccess(true);
      setTimeout(() => {
        setOverrideOpen(false);
        setOverrideSuccess(false);
        setOverrideReason("");
        setOverrideSrPk("");
      }, 1200);
    } catch (e) {
      setOverrideError(e instanceof BffError ? e.message : "Failed to apply override.");
    } finally {
      setOverrideSubmitting(false);
    }
  }

  useEffect(() => {
    async function load(): Promise<void> {
      try {
        const p = await bffFetch<Person>(`/bff/persons/${encodeURIComponent(personId)}`);
        setPerson(p);

        const [idEnv, srcEnv, salesEnv, auditEnv, matchesEnv, connsEnv, bkEnv] = await Promise.all([
          bffFetchEnvelope<PersonIdentifier[]>(`/bff/persons/${encodeURIComponent(personId)}/identifiers`).catch(() => null),
          bffFetchEnvelope<PersonSourceRecord[]>(`/bff/persons/${encodeURIComponent(personId)}/source-records`).catch(() => null),
          bffFetchEnvelope<SalesOrder[]>(`/bff/persons/${encodeURIComponent(personId)}/sales`).catch(() => null),
          bffFetchEnvelope<PersonAuditEvent[]>(`/bff/persons/${encodeURIComponent(personId)}/audit`).catch(() => null),
          bffFetchEnvelope<unknown[]>(`/bff/persons/${encodeURIComponent(personId)}/matches?limit=1`).catch(() => null),
          bffFetchEnvelope<unknown[]>(`/bff/persons/${encodeURIComponent(personId)}/connections?limit=1`).catch(() => null),
          bffFetchEnvelope<unknown[]>(`/bff/persons/${encodeURIComponent(personId)}/bankruptcy-cases?limit=1`).catch(() => null),
        ]);

        setDetailData({
          identifiers: idEnv?.data ?? [],
          sourceRecords: srcEnv?.data ?? [],
          sales: salesEnv?.data ?? [],
          audit: auditEnv?.data ?? [],
        });
        setTabTotals({
          identifier:  idEnv?.meta.total_count    ?? undefined,
          source:      srcEnv?.meta.total_count   ?? undefined,
          sales:       salesEnv?.meta.total_count ?? undefined,
          audit:       auditEnv?.meta.total_count ?? undefined,
          matches:     matchesEnv?.meta.total_count ?? undefined,
          connections: connsEnv?.meta.total_count  ?? undefined,
          bankruptcy:  bkEnv?.meta.total_count     ?? undefined,
        });
      } catch (err) {
        if (err instanceof BffError && err.status === 404) {
          setNotFoundFlag(true);
        }
      } finally {
        setLoading(false);
      }
    }

    void load();
  }, [personId]);

  const onMatchesTotal     = useCallback((n: number) => { setTabTotals((p) => ({ ...p, matches:     n })); }, []);
  const onConnectionsTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, connections: n })); }, []);
  const onIdentifierTotal  = useCallback((n: number) => { setTabTotals((p) => ({ ...p, identifier:  n })); }, []);
  const onSourceTotal      = useCallback((n: number) => { setTabTotals((p) => ({ ...p, source:      n })); }, []);
  const onSalesTotal       = useCallback((n: number) => { setTabTotals((p) => ({ ...p, sales:       n })); }, []);
  const onBankruptcyTotal  = useCallback((n: number) => { setTabTotals((p) => ({ ...p, bankruptcy:  n })); }, []);
  const onAuditTotal       = useCallback((n: number) => { setTabTotals((p) => ({ ...p, audit:       n })); }, []);

  if (notFoundFlag) notFound();

  if (loading || person === null) {
    return <PersonDetailSkeleton />;
  }

  const tabs: TabConfig[] = [
    { id: "timeline", label: "Timeline" },
    { id: "matches", label: "Matches", count: tabTotals.matches },
    { id: "connections", label: "Relations", count: tabTotals.connections ?? person.connection_count },
    { id: "identifier", label: "IDs", count: tabTotals.identifier ?? detailData.identifiers.length },
    { id: "source", label: "Sources", count: tabTotals.source ?? detailData.sourceRecords.length },
    { id: "sales", label: "Sales", count: tabTotals.sales ?? detailData.sales.length },
    { id: "bankruptcy", label: "Bankruptcy", count: tabTotals.bankruptcy },
    { id: "audit", label: "Audit", count: tabTotals.audit ?? detailData.audit.length },
    { id: "graph", label: "Graph" },
  ];

  const shell = (children: ReactElement): ReactElement => (
    <DetailShell person={person} detailData={detailData} salesTotal={tabTotals.sales} identifiersTotal={tabTotals.identifier} tabs={tabs} activeTab={activeTab} onTabChange={setActiveTab}>
      {children}
    </DetailShell>
  );

  return (
    <div className={styles.page}>
      <PersonBreadcrumb personName={person.preferred_full_name} onShare={() => void handleShare()} shareLoading={shareLoading} onOverride={() => setOverrideOpen(true)} onMerge={() => setMergeOpen(true)} />
      <div className={styles.tabContent}>
        {activeTab === "timeline" && shell(<TimelineTab person={person} detailData={detailData} />)}
        {activeTab === "matches" && shell(<MatchesTab personId={personId} onTotalLoaded={onMatchesTotal} />)}
        {activeTab === "connections" && shell(<ConnectionsTab personId={personId} onTotalLoaded={onConnectionsTotal} />)}
        {activeTab === "identifier" && shell(<IdentifiersTab personId={personId} onTotalLoaded={onIdentifierTotal} />)}
        {activeTab === "source" && shell(<SourceRecordsTab personId={personId} onTotalLoaded={onSourceTotal} />)}
        {activeTab === "sales" && shell(<SalesTab personId={personId} onTotalLoaded={onSalesTotal} />)}
        {activeTab === "bankruptcy" && shell(<BankruptcyTab personId={personId} onTotalLoaded={onBankruptcyTotal} />)}
        {activeTab === "audit" && shell(<AuditTab personId={personId} onTotalLoaded={onAuditTotal} />)}
        {activeTab === "graph" && (
          <DetailShell person={person} detailData={detailData} salesTotal={tabTotals.sales} identifiersTotal={tabTotals.identifier} tabs={tabs} activeTab={activeTab} onTabChange={setActiveTab}>
            <div style={{ height: 560, border: "1px solid var(--border)", borderRadius: "10px", overflow: "hidden" }}>
              <PersonFocusedGraph
                initialPersonId={person.person_id}
                initialTitle={person.preferred_full_name ?? person.person_id}
                overlayMode
                onMaximize={() => setGraphOpen(true)}
              />
            </div>
          </DetailShell>
        )}
      </div>

      {mergeOpen && (
        <div className={styles.shareOverlay} onClick={closeMerge}>
          <div className={styles.overrideModal} onClick={(e) => e.stopPropagation()}>
            {mergeSubmitting && <MergeLoadingOverlay />}
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Merge into another profile</span>
              <button type="button" className={styles.shareModalClose} onClick={closeMerge} aria-label="Close">×</button>
            </div>

            {mergeSuccess ? (
              <div className={styles.mergeSuccess}>
                ✓ Merged successfully. Redirecting…
              </div>
            ) : (
              <>
                <div className={styles.mergeWarning}>
                  ⚠ This action is irreversible. The current profile will be absorbed into the target profile and marked as merged.
                </div>

                {/* Search */}
                <div className={styles.overrideFieldGroup}>
                  <div className={styles.overrideLabel}>Search target profile</div>
                  <input
                    className={styles.mergeSearchInput}
                    type="text"
                    placeholder="Name, phone, email…"
                    value={mergeQuery}
                    onChange={(e) => { setMergeQuery(e.target.value); setMergeTarget(null); }}
                    autoFocus
                  />
                  {mergeSearching && <div className={styles.mergeSearchStatus}>Searching…</div>}
                  {!mergeSearching && mergeQuery.length >= 2 && mergeResults.length === 0 && (
                    <div className={styles.mergeSearchStatus}>No results found.</div>
                  )}
                  {mergeResults.length > 0 && (
                    <div className={styles.overrideSrList}>
                      {mergeResults.map((p) => (
                        <div
                          key={p.person_id}
                          className={`${styles.overrideSrRow} ${mergeTarget?.person_id === p.person_id ? styles.overrideSrRowActive : ""}`}
                          onClick={() => setMergeTarget(p)}
                          role="button"
                          tabIndex={0}
                          onKeyDown={(e) => e.key === "Enter" && setMergeTarget(p)}
                        >
                          <div className={styles.overrideSrInfo}>
                            <span className={styles.overrideSrId}>{p.preferred_full_name ?? p.person_id}</span>
                            <span className={styles.overrideSrMeta}>
                              {[p.preferred_phone, p.preferred_email].filter(Boolean).join(" · ") || p.person_id}
                            </span>
                          </div>
                          {mergeTarget?.person_id === p.person_id && <span className={styles.mergeSelectedMark}>✓</span>}
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* Preview */}
                {mergeTarget && (
                  <div className={styles.mergePreview}>
                    <span className={styles.mergePreviewFrom}>{person.preferred_full_name ?? personId}</span>
                    <span className={styles.mergePreviewArrow}>→</span>
                    <span className={styles.mergePreviewTo}>{mergeTarget.preferred_full_name ?? mergeTarget.person_id}</span>
                  </div>
                )}

                {/* Reason */}
                <div className={styles.overrideFieldGroup}>
                  <div className={styles.overrideLabel}>Reason</div>
                  <textarea
                    className={styles.overrideReason}
                    placeholder="Why are you merging these profiles?"
                    value={mergeReason}
                    onChange={(e) => setMergeReason(e.target.value)}
                    rows={2}
                  />
                </div>

                {mergeError && <div className={styles.overrideError}>{mergeError}</div>}

                <button
                  type="button"
                  className={styles.mergeSubmit}
                  onClick={() => void handleMergeSubmit()}
                  disabled={!mergeTarget || !mergeReason.trim() || mergeSubmitting}
                >
                  {mergeSubmitting ? "Merging…" : "Merge profiles"}
                </button>
              </>
            )}
          </div>
        </div>
      )}

      {overrideOpen && (
        <div className={styles.shareOverlay} onClick={() => setOverrideOpen(false)}>
          <div className={styles.overrideModal} onClick={(e) => e.stopPropagation()}>
            {overrideSubmitting && <OverrideLoadingOverlay />}
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Override field</span>
              <button type="button" className={styles.shareModalClose} onClick={() => setOverrideOpen(false)} aria-label="Close">×</button>
            </div>
            <p className={styles.shareModalDesc}>
              Pin a golden profile field to a specific source record value. This overrides the automatic survivorship selection.
            </p>

            {/* Field selector */}
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Field</div>
              <div className={styles.overrideFieldPills}>
                {([
                  { key: "full_name", label: "Full name" },
                  { key: "phone", label: "Phone" },
                  { key: "email", label: "Email" },
                  { key: "dob", label: "Date of birth" },
                  { key: "address", label: "Address" },
                ] as const).map(({ key, label }) => (
                  <button
                    key={key}
                    type="button"
                    className={`${styles.overridePill} ${overrideField === key ? styles.overridePillActive : ""}`}
                    onClick={() => { setOverrideField(key); setOverrideSrPk(""); }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            {/* Source record selector */}
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Source record</div>
              <div className={styles.overrideSrList}>
                {detailData.sourceRecords.length === 0 && (
                  <div className={styles.overrideSrEmpty}>No source records loaded. Open the Sources tab first.</div>
                )}
                {detailData.sourceRecords.map((sr) => (
                  <label key={sr.source_record_pk} className={`${styles.overrideSrRow} ${overrideSrPk === sr.source_record_pk ? styles.overrideSrRowActive : ""}`}>
                    <input
                      type="radio"
                      name="override-sr"
                      value={sr.source_record_pk}
                      checked={overrideSrPk === sr.source_record_pk}
                      onChange={() => setOverrideSrPk(sr.source_record_pk)}
                      className={styles.overrideSrRadio}
                    />
                    <div className={styles.overrideSrInfo}>
                      <span className={styles.overrideSrId}>{sr.source_record_id}</span>
                      <span className={styles.overrideSrMeta}>{sr.source_system}{sr.entity_display_name ? ` · ${sr.entity_display_name}` : ""}</span>
                    </div>
                  </label>
                ))}
              </div>
            </div>

            {/* Reason */}
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Reason</div>
              <textarea
                className={styles.overrideReason}
                placeholder="Why are you overriding this field?"
                value={overrideReason}
                onChange={(e) => setOverrideReason(e.target.value)}
                rows={2}
              />
            </div>

            {overrideError && <div className={styles.overrideError}>{overrideError}</div>}

            <button
              type="button"
              className={styles.overrideSubmit}
              onClick={() => void handleOverrideSubmit()}
              disabled={!overrideSrPk || !overrideReason.trim() || overrideSubmitting}
            >
              {overrideSuccess ? "✓ Applied" : overrideSubmitting ? "Applying…" : "Apply override"}
            </button>
          </div>
        </div>
      )}

      <PersonGraphDialog
        open={graphOpen}
        personId={person.person_id}
        title={person.preferred_full_name ?? person.person_id}
        onClose={() => setGraphOpen(false)}
      />

      {shareOpen && shareUrl && (
        <div className={styles.shareOverlay} onClick={closeShare}>
          <div className={styles.shareModal} onClick={(e) => e.stopPropagation()}>
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Share profile</span>
              <button type="button" className={styles.shareModalClose} onClick={closeShare} aria-label="Close">×</button>
            </div>
            <p className={styles.shareModalDesc}>
              Anyone with this link can view a read-only snapshot of this profile. The link expires after 30 minutes.
            </p>
            <div className={styles.shareUrlRow}>
              <input readOnly className={styles.shareUrlInput} value={shareUrl} onFocus={(e) => e.currentTarget.select()} />
              <button type="button" className={styles.shareCopyBtn} onClick={handleCopy}>
                {shareCopied ? "Copied!" : "Copy"}
              </button>
            </div>
            {shareExpiry && (
              <span className={styles.shareExpiry}>
                Expires {new Date(shareExpiry).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
