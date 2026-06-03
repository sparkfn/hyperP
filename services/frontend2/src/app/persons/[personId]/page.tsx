"use client";

import { Fragment, use, useCallback, useEffect, useId, useMemo, useRef, useState, type ReactElement } from "react";
import Link from "next/link";
import { notFound, useSearchParams } from "next/navigation";
import type { Person, PersonConnection, SalesOrder } from "@/lib/api-types";
import type {
  ChatMessage,
  GoldenProfileSelectionRequestBody,
  ManualMergeRequestBody,
  ManualMergeResponseBody,
  PersonAuditEvent,
  PersonBankruptcyCase,
  PersonIdentifier,
  PersonMatchDecision,
  PersonSharedIdentifierCandidate,
  PersonSourceRecord,
  PossibleMatchDetail,
  SharedIdentifierGroup,
  SourceRecordEntityFacet,
  SurvivorshipOverrideRequestBody,
} from "@/lib/api-types-person";
import { bffFetch, BffError, bffFetchEnvelope } from "@/lib/api-client";
import { usePaginatedFetch } from "@/lib/usePaginatedFetch";
import { toBasePath } from "@/lib/route-paths";
import type { PublicLink } from "@/lib/api-types";
import { avatarColor, completenessColor } from "@/lib/display";
import { useSetLoading } from "@/lib/LoadingContext";
import PersonFocusedGraph from "@/components/PersonFocusedGraph";
import PersonGraphDialog from "@/components/PersonGraphDialog";
import styles from "./person.module.css";

type Tab = "sales" | "connections" | "identifiers" | "source-records" | "matches" | "timeline" | "graph";

type DetailData = {
  identifiers: PersonIdentifier[];
  sourceRecords: PersonSourceRecord[];
  sales: SalesOrder[];
  audit: PersonAuditEvent[];
  bankruptcyCases: PersonBankruptcyCase[];
  sourceRecordFacets: SourceRecordEntityFacet[];
};


type TabConfig = {
  id: Tab;
  label: string;
  count?: number;
};


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
  if (Number.isNaN(d.getTime())) return "—";
  return DATE_FORMATTER.format(d);
}

function fmtDateTime(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
  return DATE_TIME_FORMATTER.format(d);
}

function fmtTime(value: string | null): string {
  if (!value) return "—";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "—";
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

/** Parses "computed-2026-05-21T23:36:59Z" → "Computed · 21 May 2026" */
type EditableField = "full_name" | "phone" | "email" | "dob" | "address";

type MergeProfileChoice = "this" | "candidate";
type MergeGoldenField = GoldenProfileSelectionRequestBody["field_name"];

interface MergeTargetProfile {
  person_id: string;
  preferred_full_name: string | null;
  preferred_phone: string | null;
  preferred_email: string | null;
  preferred_dob: string | null;
  preferred_address: Person["preferred_address"];
  preferred_nric: string | null;
}

interface MergeFieldDraft {
  key: MergeGoldenField;
  label: string;
  thisRaw: string | null;
  thisDisplay: string | null;
  candidateRaw: string | null;
  candidateDisplay: string | null;
}

/** Pull the value for the chosen field from a source record's normalized payload */
function extractSrValue(sr: PersonSourceRecord, field: EditableField): string {
  const payload = sr.normalized_payload;
  if (!payload) return "—";
  if (field === "address") {
    return payload.address?.normalized_full ?? "—";
  }
  const identifierTypeMap: Record<Exclude<EditableField, "address">, string> = {
    full_name: "full_name",
    phone: "phone",
    email: "email",
    dob: "dob",
  };
  const target = identifierTypeMap[field];
  const found = payload.identifiers?.find((id) => id.identifier_type === target);
  if (!found?.normalized_value) return "—";
  return field === "dob" ? fmtDate(found.normalized_value) : found.normalized_value;
}

/** Masks middle digits of NRIC: "S9436749B" → "S****749B" */
function maskNric(value: string | null): string {
  if (!value) return "—";
  if (value.length < 4) return value;
  const prefix = value.slice(0, 1);
  const suffix = value.slice(-3);
  const masked = "*".repeat(Math.max(0, value.length - 4));
  return `${prefix}${masked}${suffix}`;
}

function fmtGpVersion(value: string | null): string {
  if (!value) return "—";
  const match = /^([a-z_]+)-(\d{4}-\d{2}-\d{2}(?:T.+)?)$/i.exec(value);
  if (!match || !match[1] || !match[2]) return value;
  const type = match[1].charAt(0).toUpperCase() + match[1].slice(1);
  const date = fmtDate(match[2] ?? null);
  return `${type} · ${date}`;
}

function CopyableId({ value }: { value: string }): ReactElement {
  const [copied, setCopied] = useState(false);
  const short = `${value.slice(0, 8)}…${value.slice(-5)}`;

  function handleCopy(): void {
    void navigator.clipboard.writeText(value).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    });
  }

  return (
    <div className={styles.copyableId}>
      <span className={`${styles.sidebarHeroSummaryValue} ${styles.mono}`} title={value}>{short}</span>
      <button type="button" className={styles.copyBtn} onClick={handleCopy} aria-label="Copy ID">
        {copied ? (
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M20 6L9 17l-5-5" />
          </svg>
        ) : (
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <rect x="9" y="9" width="13" height="13" rx="2" />
            <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
          </svg>
        )}
      </button>
    </div>
  );
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

function PersonBreadcrumb({ personName, onShare, shareLoading }: { personName: string | null; onShare: () => void; shareLoading: boolean }): ReactElement {
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
          <span className={styles.btnLabel}>{shareLoading ? "Generating…" : "Share"}</span>
        </button>
      </div>
    </div>
  );
}

function PersonSidebar({ person, detailData, onOverride }: { person: Person; detailData: DetailData; onOverride: () => void }): ReactElement {
  const [detailOpen, setDetailOpen] = useState(false);
  const [nricRevealed, setNricRevealed] = useState(false);
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

  const phone = person.preferred_phone ?? null;
  const email = person.preferred_email ?? null;
  const address = person.preferred_address?.normalized_full ?? null;

  const gpFields: Array<{ label: string; value: string; mono?: boolean; revealable?: boolean }> = [
    { label: "NRIC",    value: nricRevealed ? (person.preferred_nric ?? "—") : maskNric(person.preferred_nric ?? null), mono: true, revealable: true },
    { label: "DOB",     value: person.preferred_dob ? fmtDate(person.preferred_dob) : "—" },
    { label: "Phone",   value: phone  ?? "—" },
    { label: "Email",   value: email  ?? "—" },
    { label: "Address", value: address ?? "—" },
  ];

  const detailRows: Array<{ label: string; value: string; mono?: boolean; copyable?: boolean }> = [
    { label: "Person ID",    value: person.person_id, mono: true, copyable: true },
    { label: "Created",      value: fmtDateTime(person.created_at) },
    { label: "Last updated", value: fmtDateTime(person.updated_at) },
    { label: "GP version",   value: fmtGpVersion(person.golden_profile_version) },
  ];

  return (
    <aside className={styles.sidebar}>
      <section className={styles.sidebarHeroCard}>

        {/* ── Section 1: Profile ── */}
        <div className={styles.profileHero}>
          <div
            className={styles.avatarRing}
            style={{ background: `conic-gradient(${completenessColor(person.profile_completeness_score)} ${completeness}%, rgba(148, 163, 184, 0.18) 0)` }}
          >
            <div className={styles.avatarRingInner}>
              <div className={styles.avatar} style={{ background: avatarColor(person.preferred_full_name ?? "?") }}>
                {personInitials(person.preferred_full_name)}
              </div>
            </div>
          </div>

          <div className={styles.profileHeroName}>{person.preferred_full_name ?? "—"}</div>

          <div className={styles.profileHeroBadges}>
            <span className={`${styles.badge} ${statusClass}`}>{titleCase(person.status)}</span>
            {priorityLabel !== null && (
              <span className={`${styles.priorityLabel} ${priorityLabel.className}`}>{priorityLabel.label}</span>
            )}
          </div>

          <div className={styles.profileHeroCompleteness}>
            <div className={styles.profileHeroBar}>
              <div
                className={styles.completenessFill}
                style={{ width: `${completeness}%`, background: completenessColor(person.profile_completeness_score) }}
              />
            </div>
            <span className={styles.profileHeroCompPct}>{completeness}%</span>
          </div>

          <button type="button" className={styles.editFieldBtn} onClick={onOverride}>
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
              <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
            </svg>
            Edit field
          </button>
        </div>

        {/* ── Section 2: Golden profile fields ── */}
        <div className={styles.profileSection}>
          <div className={styles.sidebarHeroSummaryRows}>
            {gpFields.map((field) => (
              <div key={field.label} className={styles.sidebarHeroSummaryRow}>
                <div className={styles.sidebarHeroSummaryLabel}>{field.label}</div>
                {field.revealable === true ? (
                  <div className={styles.revealableField}>
                    <span className={`${styles.sidebarHeroSummaryValue} ${styles.mono}`}>{field.value}</span>
                    <button
                      type="button"
                      className={styles.revealBtn}
                      onClick={() => setNricRevealed((v) => !v)}
                      aria-label={nricRevealed ? "Hide NRIC" : "Show NRIC"}
                    >
                      {nricRevealed ? (
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
                          <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
                          <line x1="1" y1="1" x2="23" y2="23"/>
                        </svg>
                      ) : (
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                          <circle cx="12" cy="12" r="3"/>
                        </svg>
                      )}
                    </button>
                  </div>
                ) : (
                  <div className={`${styles.sidebarHeroSummaryValue} ${field.mono === true ? styles.mono : ""}`}>{field.value}</div>
                )}
              </div>
            ))}
          </div>
        </div>

        {/* ── Section 3: Detail (collapsible) ── */}
        <div className={styles.profileSection}>
          <button
            type="button"
            className={styles.profileSectionToggle}
            onClick={() => setDetailOpen((o) => !o)}
            aria-expanded={detailOpen}
          >
            <span className={styles.profileSectionLabel}>Detail</span>
            <svg
              className={`${styles.profileSectionChevron} ${detailOpen ? styles.profileSectionChevronOpen : ""}`}
              width="12" height="12" viewBox="0 0 24 24" fill="none"
              stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"
              aria-hidden="true"
            >
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
          {detailOpen && (
            <div className={styles.sidebarHeroSummaryRows}>
              {detailRows.map((field) => (
                <div key={field.label} className={styles.sidebarHeroSummaryRow}>
                  <div className={styles.sidebarHeroSummaryLabel}>{field.label}</div>
                  {field.copyable === true ? (
                    <CopyableId value={field.value} />
                  ) : (
                    <div className={`${styles.sidebarHeroSummaryValue} ${field.mono === true ? styles.mono : ""}`}>{field.value}</div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

      </section>

      <BankruptcySidebarCard cases={detailData.bankruptcyCases} />
      <SourceEntitySidebarCard sourceRecords={detailData.sourceRecords} />
    </aside>
  );
}

function BankruptcyCaseRow({ bk }: { bk: PersonBankruptcyCase }): ReactElement {
  const [open, setOpen] = useState(false);
  const fields: Array<[string, string | null]> = [
    ["Case number",    bk.case_number],
    ["Source case ID", bk.source_case_id],
    ["Source system",  bk.source_system_key],
    ["Document type",  bk.document_type ? titleCase(bk.document_type) : null],
    ["Document date",  bk.document_date ? fmtDate(bk.document_date) : null],
    ["Event type",     bk.event_type ? titleCase(bk.event_type) : null],
    ["Event date",     bk.event_date ? fmtDate(bk.event_date) : null],
    ["Trustee",        bk.trustee_name],
    ["Trustee firm",   bk.trustee_firm],
    ["First seen",     bk.first_seen_at ? fmtDate(bk.first_seen_at) : null],
    ["Last seen",      bk.last_seen_at ? fmtDate(bk.last_seen_at) : null],
  ];

  return (
    <div className={styles.bkCaseBlock}>
      <button
        type="button"
        className={styles.bkCaseToggle}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className={styles.bkCaseLabel}>{bk.case_number ?? bk.source_case_id}</span>
        <svg
          className={`${styles.bkChevron} ${open ? styles.bkChevronOpen : ""}`}
          width="12" height="12" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="2.5" aria-hidden="true"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>
      {open && (
        <div className={styles.bkFieldGrid}>
          {fields.map(([label, value]) => value ? (
            <div key={label} className={styles.bkFieldRow}>
              <span className={styles.bkFieldLabel}>{label}</span>
              <span className={styles.bkFieldValue}>{value}</span>
            </div>
          ) : null)}
          {bk.source_url && (
            <div className={styles.bkFieldRow}>
              <span className={styles.bkFieldLabel}>Source</span>
              <a
                href={bk.source_url.startsWith("http") ? bk.source_url : "#"}
                target="_blank"
                rel="noopener noreferrer"
                className={styles.bkFieldLink}
              >
                View source ↗
              </a>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function BankruptcySidebarCard({ cases }: { cases: PersonBankruptcyCase[] }): ReactElement {
  const hasCases = cases.length > 0;

  if (!hasCases) {
    return (
      <section className={styles.sidebarCard}>
        <div className={styles.bkHeader}>
          <span className={styles.bkSectionLabel}>Bankruptcy</span>
          <span className={styles.bkBadgeClear}>✓ Clear</span>
        </div>
        <p className={styles.bkClearBody}>No bankruptcy record found.</p>
      </section>
    );
  }

  return (
    <section className={styles.sidebarCard}>
      <div className={styles.bkHeader}>
        <span className={styles.bkSectionLabel}>Bankruptcy</span>
        <span className={styles.bkBadgeDanger}>⚠ {cases.length} {cases.length === 1 ? "case" : "cases"}</span>
      </div>
      <div className={styles.bkCaseList}>
        {cases.map((bk) => (
          <BankruptcyCaseRow key={bk.bankruptcy_case_id} bk={bk} />
        ))}
      </div>
    </section>
  );
}

interface SourceEntitySummary {
  key: string;
  sourceSystem: string;
  entityDisplayName: string | null;
  entityKey: string | null;
  count: number;
}

function buildSourceEntitySummaries(sourceRecords: PersonSourceRecord[]): SourceEntitySummary[] {
  const summaries = new Map<string, SourceEntitySummary>();

  sourceRecords.forEach((sourceRecord) => {
    const key = [sourceRecord.source_system, sourceRecord.entity_key ?? "", sourceRecord.entity_display_name ?? ""].join("::");
    const existing = summaries.get(key);

    if (existing !== undefined) {
      existing.count += 1;
      return;
    }

    summaries.set(key, {
      key,
      sourceSystem: sourceRecord.source_system,
      entityDisplayName: sourceRecord.entity_display_name,
      entityKey: sourceRecord.entity_key,
      count: 1,
    });
  });

  return Array.from(summaries.values()).sort((a, b) => {
    const sourceCompare = a.sourceSystem.localeCompare(b.sourceSystem);
    if (sourceCompare !== 0) return sourceCompare;
    return (a.entityDisplayName ?? a.entityKey ?? "").localeCompare(b.entityDisplayName ?? b.entityKey ?? "");
  });
}

function SourceEntitySidebarCard({ sourceRecords }: { sourceRecords: PersonSourceRecord[] }): ReactElement {
  const summaries = buildSourceEntitySummaries(sourceRecords);
  const recordLabel = `${sourceRecords.length} ${sourceRecords.length === 1 ? "record" : "records"}`;

  return (
    <section className={styles.sidebarCard}>
      <div className={styles.sourceEntityHeader}>
        <span className={styles.bkSectionLabel}>Sources / Entity</span>
        <span className={styles.sourceEntityBadge}>{recordLabel}</span>
      </div>

      {summaries.length === 0 ? (
        <p className={styles.sourceEntityEmpty}>No source records linked.</p>
      ) : (
        <div className={styles.sourceEntityList}>
          {summaries.map((summary) => {
            const meta = titleCase(summary.sourceSystem);
            const countLabel = `${summary.count} ${summary.count === 1 ? "record" : "records"}`;

            return (
              <div key={summary.key} className={styles.sourceEntityRow}>
                <div className={styles.sourceEntityRowTop}>
                  <div className={styles.sourceEntityName}>{summary.entityDisplayName ?? summary.entityKey ?? "Unknown entity"}</div>
                  <div className={styles.sourceEntityCount}>{countLabel}</div>
                </div>
                <div className={styles.sourceEntityMeta}>{meta}</div>
              </div>
            );
          })}
        </div>
      )}
    </section>
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

function RightRail({ person, detailData, salesTotal, tabs, activeTab, onChange, children }: { person: Person; detailData: DetailData; salesTotal: number | undefined; tabs: TabConfig[]; activeTab: Tab; onChange: (tab: Tab) => void; children: ReactElement }): ReactElement {
  const totalSales = detailData.sales.reduce((sum, order) => sum + (order.total_amount ?? 0), 0);
  const completeness = Math.round(person.profile_completeness_score * 100);
  const latestActivityAt = detailData.sourceRecords[0]?.observed_at ?? person.updated_at;
  const salesCount = salesTotal ?? detailData.sales.length;
  const idCount = detailData.identifiers.length;

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
      valueStyle: { color: completenessColor(person.profile_completeness_score) },
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

type TimelineSubTab = "activity" | "audit";

function TimelineTab({ person, detailData, personId }: { person: Person; detailData: DetailData; personId: string }): ReactElement {
  const [subTab, setSubTab] = useState<TimelineSubTab>("activity");

  return (
    <section className={styles.contentCard}>
      <div className={styles.innerTabBar}>
        <button
          type="button"
          className={`${styles.innerTab} ${subTab === "activity" ? styles.innerTabActive : ""}`}
          onClick={() => setSubTab("activity")}
        >Activity</button>
        <button
          type="button"
          className={`${styles.innerTab} ${subTab === "audit" ? styles.innerTabActive : ""}`}
          onClick={() => setSubTab("audit")}
        >Audit</button>
      </div>
      {subTab === "activity"
        ? <TimelineActivity person={person} detailData={detailData} />
        : <AuditTab personId={personId} onTotalLoaded={() => { /* count shown inside */ }} />
      }
    </section>
  );
}

function TimelineActivity({ person, detailData }: { person: Person; detailData: DetailData }): ReactElement {
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
    <>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Activity</span>
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
    </>
  );
}

function DetailShell({ person, detailData, salesTotal, children, tabs, activeTab, onTabChange, onOverride }: { person: Person; detailData: DetailData; salesTotal: number | undefined; children: ReactElement; tabs: TabConfig[]; activeTab: Tab; onTabChange: (tab: Tab) => void; onOverride: () => void }): ReactElement {
  return (
    <div className={styles.detailLayout}>
      <PersonSidebar person={person} detailData={detailData} onOverride={onOverride} />
      <RightRail person={person} detailData={detailData} salesTotal={salesTotal} tabs={tabs} activeTab={activeTab} onChange={onTabChange}>
        {children}
      </RightRail>
    </div>
  );
}



const SKEL_N = [0, 1, 2, 3, 4] as const;

function MergeLoadingOverlay(): ReactElement {
  return (
    <div className={styles.modalLoadingOverlay}>
      <svg className={styles.modalLoadingSpinner} viewBox="0 0 100 100" overflow="visible" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
        {/* System A — whole group translates: left edge → home (28,50) → center (50,50) */}
        <g>
          <animateTransform attributeName="transform" type="translate" values="-23 0;0 0;0 0;22 0;22 0;-23 0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0" />
          <circle cx="28" cy="50" r="12" fill="none" stroke="currentColor" strokeWidth="0.4">
            <animate attributeName="opacity" values="0;0;0.22;0.22;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
          </circle>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="0 28 50" to="360 28 50" dur="2.4s" repeatCount="indefinite" />
            <line x1="28" y1="50" x2="40" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="40" cy="50" r="3.5" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.60;0.60;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="180 28 50" to="540 28 50" dur="2.4s" repeatCount="indefinite" />
            <line x1="28" y1="50" x2="40" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="40" cy="50" r="3" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.42;0.42;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <circle cx="28" cy="50" r="6" fill="currentColor">
            <animate attributeName="opacity" values="0;0.55;0.55;0;0;0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        {/* System B — whole group translates: right edge → home (72,50) → center (50,50) */}
        <g>
          <animateTransform attributeName="transform" type="translate" values="23 0;0 0;0 0;-22 0;-22 0;23 0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" calcMode="spline" keySplines="0.4 0 0.2 1;0 0 0 0;0.4 0 0.2 1;0 0 0 0;0 0 0 0" />
          <circle cx="72" cy="50" r="12" fill="none" stroke="currentColor" strokeWidth="0.4">
            <animate attributeName="opacity" values="0;0;0.22;0.22;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
          </circle>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="0 72 50" to="360 72 50" dur="2s" repeatCount="indefinite" />
            <line x1="72" y1="50" x2="84" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="84" cy="50" r="3.5" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.60;0.60;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="240 72 50" to="600 72 50" dur="2s" repeatCount="indefinite" />
            <line x1="72" y1="50" x2="84" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="84" cy="50" r="3" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.42;0.42;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <g>
            <animateTransform attributeName="transform" type="rotate" from="120 72 50" to="480 72 50" dur="2s" repeatCount="indefinite" />
            <line x1="72" y1="50" x2="84" y2="50" stroke="currentColor" strokeWidth="0.5">
              <animate attributeName="opacity" values="0;0;0.28;0.28;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </line>
            <circle cx="84" cy="50" r="2.5" fill="currentColor">
              <animate attributeName="opacity" values="0;0;0.35;0.35;0;0" keyTimes="0;0.06;0.09;0.12;0.20;1" dur="8s" repeatCount="indefinite" />
            </circle>
          </g>
          <circle cx="72" cy="50" r="6" fill="currentColor">
            <animate attributeName="opacity" values="0;0.55;0.55;0;0;0" keyTimes="0;0.07;0.09;0.20;0.90;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        {/* Merged state — center (50,50), 2 rings */}
        <circle cx="50" cy="50" r="0" fill="none" stroke="currentColor" strokeWidth="1.5">
          <animate attributeName="r"       values="0;0;8;38;0;0" keyTimes="0;0.18;0.21;0.34;0.38;1" dur="8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.5;0;0;0"  keyTimes="0;0.18;0.21;0.34;0.38;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="0" fill="currentColor">
          <animate attributeName="r"       values="0;0;8;8;0;0"     keyTimes="0;0.18;0.24;0.82;0.90;1" dur="8s" repeatCount="indefinite" />
          <animate attributeName="opacity" values="0;0;0.7;0.7;0;0" keyTimes="0;0.18;0.24;0.82;0.90;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="22" fill="none" stroke="currentColor" strokeWidth="0.4">
          <animate attributeName="opacity" values="0;0;0;0.25;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <circle cx="50" cy="50" r="36" fill="none" stroke="currentColor" strokeWidth="0.3">
          <animate attributeName="opacity" values="0;0;0;0.18;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
        </circle>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="0 50 50" to="360 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7">
            <animate attributeName="opacity" values="0;0;0;0.30;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="72" cy="50" r="5" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.65;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="540 50 50" dur="3s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="72" y2="50" stroke="currentColor" strokeWidth="0.7">
            <animate attributeName="opacity" values="0;0;0;0.30;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="72" cy="50" r="4" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.50;0;0" keyTimes="0;0.18;0.26;0.30;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="60 50 50" to="-300 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5">
            <animate attributeName="opacity" values="0;0;0;0.22;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="86" cy="50" r="4" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.55;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="180 50 50" to="-180 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5">
            <animate attributeName="opacity" values="0;0;0;0.22;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="86" cy="50" r="3.5" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.45;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </circle>
        </g>
        <g>
          <animateTransform attributeName="transform" type="rotate" from="300 50 50" to="-60 50 50" dur="5s" repeatCount="indefinite" />
          <line x1="50" y1="50" x2="86" y2="50" stroke="currentColor" strokeWidth="0.5">
            <animate attributeName="opacity" values="0;0;0;0.22;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
          </line>
          <circle cx="86" cy="50" r="3" fill="currentColor">
            <animate attributeName="opacity" values="0;0;0;0.40;0;0" keyTimes="0;0.18;0.28;0.32;0.84;1" dur="8s" repeatCount="indefinite" />
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
      <svg className={styles.tabEmptyIcon} viewBox="0 0 80 80" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        {/* Center group — rings + stubs + main node float together */}
        <g>
          <animateTransform attributeName="transform" type="translate" values="0 0;0 -5;0 0" dur="3.4s" begin="0s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" />
          <circle cx="38" cy="39" r="16" stroke="currentColor" strokeWidth="0.7" strokeDasharray="5 4 12 2 8 5 3 6" opacity="0.18" />
          <circle cx="38" cy="39" r="27" stroke="currentColor" strokeWidth="0.5" strokeDasharray="8 6 3 11 7 4 14 5" opacity="0.10" />
          <line x1="38" y1="39" x2="18" y2="19" stroke="currentColor" strokeWidth="0.9" strokeDasharray="2 2.5" strokeLinecap="round" opacity="0.22" />
          <line x1="38" y1="39" x2="61" y2="27" stroke="currentColor" strokeWidth="0.8" strokeDasharray="2 2.5" strokeLinecap="round" opacity="0.15" />
          <line x1="38" y1="39" x2="61" y2="57" stroke="currentColor" strokeWidth="0.8" strokeDasharray="2 2.5" strokeLinecap="round" opacity="0.13" />
          <line x1="38" y1="39" x2="20" y2="58" stroke="currentColor" strokeWidth="0.7" strokeDasharray="1.5 3"   strokeLinecap="round" opacity="0.11" />
          <circle cx="38" cy="39" r="6.5" fill="currentColor" opacity="0.42" />
        </g>
        {/* Debris — each floats at its own pace */}
        <circle cx="7"  cy="11" r="3"   fill="currentColor" opacity="0.24"><animateTransform attributeName="transform" type="translate" values="0 0;0 -4;0 0" dur="4.1s" begin="-1.2s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="70" cy="8"  r="2"   fill="currentColor" opacity="0.17"><animateTransform attributeName="transform" type="translate" values="0 0;0 -3;0 0" dur="3.7s" begin="-2.8s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="73" cy="61" r="2.5" fill="currentColor" opacity="0.19"><animateTransform attributeName="transform" type="translate" values="0 0;0 -3.5;0 0" dur="4.6s" begin="-0.7s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="5"  cy="66" r="1.8" fill="currentColor" opacity="0.15"><animateTransform attributeName="transform" type="translate" values="0 0;0 -2.5;0 0" dur="3.2s" begin="-1.9s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="53" cy="74" r="1.5" fill="currentColor" opacity="0.13"><animateTransform attributeName="transform" type="translate" values="0 0;0 -2;0 0"   dur="4.8s" begin="-3.1s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="17" cy="73" r="1"   fill="currentColor" opacity="0.10"><animateTransform attributeName="transform" type="translate" values="0 0;0 -2;0 0"   dur="3.9s" begin="-0.4s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="66" cy="43" r="1.2" fill="currentColor" opacity="0.12"><animateTransform attributeName="transform" type="translate" values="0 0;0 -3;0 0"   dur="4.3s" begin="-2.2s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
        <circle cx="29" cy="4"  r="1.3" fill="currentColor" opacity="0.11"><animateTransform attributeName="transform" type="translate" values="0 0;0 -2.5;0 0" dur="3.6s" begin="-1.6s" calcMode="spline" keySplines="0.45 0 0.55 1;0.45 0 0.55 1" repeatCount="indefinite" /></circle>
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

function sourceRecordMeta(record: PersonSourceRecord): string {
  return [record.entity_display_name ?? record.source_system, record.source_record_id].filter(Boolean).join(" · ");
}

function sourceRecordEvidence(record: PersonSourceRecord, group: SharedIdentifierGroup): string {
  const identifier = record.normalized_payload?.identifiers?.find(
    (item) => item.identifier_type === group.identifier_type && item.normalized_value === group.normalized_value,
  );
  if (identifier?.quality_flag) return identifier.quality_flag;
  if (record.record_type === "conversation" && record.extraction_confidence != null) {
    return `${Math.round(record.extraction_confidence * 100)}% extraction confidence`;
  }
  return titleCase(record.record_type);
}

function CandidateSourceRecords({ title, records, group }: { title: string; records: PersonSourceRecord[]; group: SharedIdentifierGroup }): ReactElement {
  return (
    <div className={styles.matchSourceColumn}>
      <span className={styles.matchSourceTitle}>{title}</span>
      {records.length === 0 ? (
        <span className={styles.matchSourceEmpty}>No source record shown</span>
      ) : records.map((record) => (
        <div key={record.source_record_pk} className={styles.matchSourceRecord}>
          <span className={styles.matchSourceMeta}>{sourceRecordMeta(record)}</span>
          <span className={styles.matchSourceSub}>{sourceRecordEvidence(record, group)} · observed {fmtDate(record.observed_at)}</span>
        </div>
      ))}
    </div>
  );
}

function CandidateDetailPanel({ detail }: { detail: PossibleMatchDetail }): ReactElement {
  return (
    <div className={styles.candidateDetailPanel}>
      <div className={styles.candidateWhyHeader}>
        <span>Why this is a possible duplicate</span>
        <Link href={`/persons/${detail.candidate_person_id}`} className={styles.candidateProfileLink}>Open candidate profile</Link>
      </div>
      <div className={styles.candidateReasonList}>
        {detail.shared_identifier_groups.map((group) => (
          <div key={`${group.identifier_type}-${group.normalized_value}`} className={styles.candidateReasonCard}>
            <div className={styles.candidateReasonTop}>
              <span className={styles.candidateReasonLabel}>{titleCase(group.identifier_type)}</span>
              <span className={`${styles.candidateReasonValue} ${styles.mono}`}>{group.normalized_value}</span>
            </div>
            <div className={styles.matchSourceGrid}>
              <CandidateSourceRecords title="Current person data" records={group.current_person_source_records} group={group} />
              <CandidateSourceRecords title="Candidate data" records={group.candidate_source_records} group={group} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CandidateRow({
  candidate,
  detail,
  error,
  isLoading,
  isOpen,
  onToggle,
  onMerge,
}: {
  candidate: PersonSharedIdentifierCandidate;
  detail: PossibleMatchDetail | undefined;
  error: string | undefined;
  isLoading: boolean;
  isOpen: boolean;
  onToggle: (candidateId: string) => void;
  onMerge: () => void;
}): ReactElement {
  return (
    <div className={`${styles.candidateCard} ${isOpen ? styles.candidateCardOpen : ""}`}>
      <div className={styles.candidateRowShell}>
        <button type="button" className={styles.candidateRowButton} onClick={() => onToggle(candidate.person_id)} aria-expanded={isOpen}>
          <div className={styles.sharedAvatar} style={{ background: avatarColor(candidate.preferred_full_name ?? "?") }}>
            {personInitials(candidate.preferred_full_name)}
          </div>
          <div className={styles.connBody}>
            <div className={styles.sharedName}>{candidate.preferred_full_name ?? candidate.person_id}</div>
            <div className={styles.connMetaRow}>
              {candidate.identifiers.slice(0, 3).map((id, j) => (
                <span key={j} className={styles.connSourceChip}>
                  <span className={styles.connSourceKey}>{titleCase(id.identifier_type)}: {id.normalized_value}</span>
                </span>
              ))}
              {candidate.identifiers.length > 3 && (
                <span className={styles.connSourceChip}>
                  <span className={styles.connSourceKey}>+{candidate.identifiers.length - 3} more</span>
                </span>
              )}
            </div>
          </div>
          <span className={candidate.identifier_strength === "strong" ? styles.strengthStrong : styles.strengthWeak}>
            {candidate.identifier_strength === "strong" ? "Strong match" : "Possible"}
          </span>
        </button>
        <div className={styles.candidateRowActions}>
          <Link href={`/persons/${candidate.person_id}`} className={styles.candidateDirectLink}>
            Direct link
          </Link>
          <button type="button" className={styles.candidateMergeBtn} onClick={onMerge}>
            Merge
          </button>
          <button type="button" className={styles.candidateExpandButton} onClick={() => onToggle(candidate.person_id)} aria-expanded={isOpen} aria-label={isOpen ? "Hide match reasons" : "Show match reasons"}>
            <svg className={styles.candidateChevron} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
        </div>
      </div>
      {isOpen && (
        isLoading ? <div className={styles.candidateDetailStatus}>Loading match reasons…</div>
          : error ? <div className={styles.candidateDetailError}>{error}</div>
            : detail ? <CandidateDetailPanel detail={detail} />
              : <div className={styles.candidateDetailStatus}>No detail loaded.</div>
      )}
    </div>
  );
}

function MatchesTab({ personId, onTotalLoaded, onMergeWith }: { personId: string; onTotalLoaded: (n: number) => void; onMergeWith: (candidate: PersonSharedIdentifierCandidate) => void }): ReactElement {
  const [expandedMatch, setExpandedMatch] = useState<string | null>(null);
  const [expandedCandidate, setExpandedCandidate] = useState<string | null>(null);
  const [candidateDetails, setCandidateDetails] = useState<Record<string, PossibleMatchDetail>>({});
  const [candidateErrors, setCandidateErrors] = useState<Record<string, string>>({});
  const [loadingCandidateId, setLoadingCandidateId] = useState<string | null>(null);

  const candidatesResult = usePaginatedFetch<PersonSharedIdentifierCandidate>(
    `/bff/persons/${encodeURIComponent(personId)}/shared-identifiers`,
  );
  const decisionsResult = usePaginatedFetch<PersonMatchDecision>(
    `/bff/persons/${encodeURIComponent(personId)}/matches`,
  );

  const candidates = candidatesResult.rows ?? [];
  const decisions  = decisionsResult.rows  ?? [];

  useEffect(() => {
    if (!candidatesResult.loading && !decisionsResult.loading) {
      onTotalLoaded(
        (candidatesResult.total ?? candidates.length) +
        (decisionsResult.total  ?? decisions.length),
      );
    }
  }, [
    candidatesResult.loading, candidatesResult.total,
    decisionsResult.loading,  decisionsResult.total,
    candidates.length, decisions.length, onTotalLoaded,
  ]);

  const toggleCandidate = useCallback((candidateId: string) => {
    if (expandedCandidate === candidateId) {
      setExpandedCandidate(null);
      return;
    }
    setExpandedCandidate(candidateId);
    if (candidateDetails[candidateId] || loadingCandidateId === candidateId) return;

    setLoadingCandidateId(candidateId);
    setCandidateErrors((prev) => {
      if (!(candidateId in prev)) return prev;
      const next = { ...prev };
      delete next[candidateId];
      return next;
    });
    void bffFetch<PossibleMatchDetail>(
      `/bff/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidateId)}/detail`,
    ).then((detail) => {
      setCandidateDetails((prev) => ({ ...prev, [candidateId]: detail }));
    }).catch((err: unknown) => {
      const message = err instanceof BffError ? err.message : "Could not load match reasons.";
      setCandidateErrors((prev) => ({ ...prev, [candidateId]: message }));
    }).finally(() => {
      setLoadingCandidateId((current) => current === candidateId ? null : current);
    });
  }, [candidateDetails, expandedCandidate, loadingCandidateId, personId]);

  const allLoading = candidatesResult.loading && decisionsResult.loading;
  if (allLoading) return <TabSkelShell title="Matches"><SkeletonMatches /></TabSkelShell>;
  if (candidatesResult.error && decisionsResult.error) {
    return <section className={styles.contentCard}><div className={styles.tabError}>{candidatesResult.error}</div></section>;
  }

  const combinedTotal = (candidatesResult.total ?? candidates.length) + (decisionsResult.total ?? decisions.length);

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Matches</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{combinedTotal} total</span>
      </div>

      {/* ── Possible Duplicates ── */}
      {(candidatesResult.loading || candidates.length > 0) && (
        <div className={styles.connSection}>
          <div className={styles.connSectionLabel}>
            Possible Duplicates
            <span className={styles.connSectionCount}>{candidatesResult.total ?? candidates.length}</span>
          </div>
          {candidatesResult.loading ? <SkeletonConnections /> : (
            <>
              <div className={styles.sharedList}>
                {candidates.map((c) => (
                  <CandidateRow
                    key={c.person_id}
                    candidate={c}
                    detail={candidateDetails[c.person_id]}
                    error={candidateErrors[c.person_id]}
                    isLoading={loadingCandidateId === c.person_id}
                    isOpen={expandedCandidate === c.person_id}
                    onToggle={toggleCandidate}
                    onMerge={() => onMergeWith(c)}
                  />
                ))}
              </div>
              {(candidatesResult.hasPrev || candidatesResult.hasNext) && (
                <TabPagination from={candidatesResult.from} to={candidatesResult.to} total={candidatesResult.total} hasPrev={candidatesResult.hasPrev} hasNext={candidatesResult.hasNext} onPrev={candidatesResult.goPrev} onNext={candidatesResult.goNext} />
              )}
            </>
          )}
        </div>
      )}

      {/* ── Decision History ── */}
      {(decisionsResult.loading || decisions.length > 0) && (
        <div className={styles.connSection}>
          <div className={styles.connSectionLabel}>
            Decision History
            <span className={styles.connSectionCount}>{decisionsResult.total ?? decisions.length}</span>
          </div>
          {decisionsResult.loading ? <SkeletonMatches /> : (
            <>
              <div className={styles.matchList}>
                <div className={styles.matchHeaderRow}>
                  <span>Decision</span>
                  <span>Engine</span>
                  <span>Counterpart</span>
                  <span>Confidence</span>
                  <span>Created</span>
                </div>
                {decisions.map((match) => {
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
                              { label: "Policy",    value: match.policy_version },
                              { label: "Left",      value: match.left_person_id },
                              { label: "Right",     value: match.right_person_id },
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
              {(decisionsResult.hasPrev || decisionsResult.hasNext) && (
                <TabPagination from={decisionsResult.from} to={decisionsResult.to} total={decisionsResult.total} hasPrev={decisionsResult.hasPrev} hasNext={decisionsResult.hasNext} onPrev={decisionsResult.goPrev} onNext={decisionsResult.goNext} />
              )}
            </>
          )}
        </div>
      )}

      {/* Both empty */}
      {!candidatesResult.loading && !decisionsResult.loading && candidates.length === 0 && decisions.length === 0 && (
        <TabEmptyState message="No match data on record." />
      )}
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

  if (loading) return <><div className={styles.connHeader}><span className={styles.connHeaderTitle}>Audit trail</span></div><SkeletonAudit /></>;
  if (error) return <div className={styles.tabError}>{error}</div>;

  return (
    <>
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
    </>
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
    <span key="hops">{hops} record{hops !== 1 ? "s" : ""}</span>,
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


function srEntityLabel(facet: SourceRecordEntityFacet): string {
  return facet.entity_display_name ?? facet.entity_key ?? titleCase(facet.source_system);
}

/** Staff/agent/sender messages align right; everyone else (customer/prospect/unknown) left. */
function srChatSide(role: string | null): "agent" | "customer" {
  if (role === null) return "customer";
  const r = role.toLowerCase();
  return r.includes("staff") || r.includes("agent") || r.includes("sender") ? "agent" : "customer";
}

/** Distinct bubble colors assigned per speaker, by order of first appearance — so
 *  several unknown-role speakers (all on the left) stay visually distinguishable. */
const CHAT_BUBBLE_COLORS = ["#4361ee", "#7c3aed", "#0891b2", "#059669", "#d97706", "#db2777", "#0d9488", "#9333ea"] as const;
const CHAT_FALLBACK_COLOR = "#4361ee";

function buildSpeakerColors(messages: ChatMessage[]): Map<string, string> {
  const map = new Map<string, string>();
  messages.forEach((m) => {
    if (!map.has(m.speaker)) {
      map.set(m.speaker, CHAT_BUBBLE_COLORS[map.size % CHAT_BUBBLE_COLORS.length] ?? CHAT_FALLBACK_COLOR);
    }
  });
  return map;
}

function SourceRecordRow({ record }: { record: PersonSourceRecord }): ReactElement {
  const [open, setOpen] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);
  const speakerColors = useMemo(() => buildSpeakerColors(record.chat_transcript ?? []), [record.chat_transcript]);
  const entity = record.entity_display_name ?? record.entity_key ?? "Unknown entity";
  const payload = record.normalized_payload;
  const identifiers = payload?.identifiers ?? [];
  const attributes = payload?.attributes ?? [];
  const address = payload?.address?.normalized_full ?? null;

  const meta: Array<[string, string]> = [
    ["Source system", titleCase(record.source_system)],
    ["Entity", entity],
    ["Record ID", record.source_record_id],
    ["Version", record.source_record_version ?? "—"],
    ["Type", titleCase(record.record_type)],
    ["Link status", titleCase(record.link_status)],
    ["Observed", record.observed_at_display || "—"],
    ["Ingested", record.ingested_at_display || "—"],
    ["Record PK", record.source_record_pk],
  ];
  if (record.record_type === "conversation") {
    if (record.extraction_method) meta.push(["Extraction", titleCase(record.extraction_method)]);
    if (record.extraction_confidence_display) meta.push(["Confidence", record.extraction_confidence_display]);
  }

  return (
    <div className={`${styles.srcRow} ${open ? styles.srcRowOpen : ""}`}>
      <div
        className={styles.srcMain}
        onClick={() => setOpen((v) => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && setOpen((v) => !v)}
      >
        <div className={styles.idBody}>
          <span className={styles.idValue}>{titleCase(record.source_system)}</span>
          <div className={styles.connMeta}>
            <span>{entity}</span>
            <span className={styles.connMetaSep}>·</span>
            <span>{record.source_record_id}</span>
            {record.source_record_version && (
              <><span className={styles.connMetaSep}>·</span><span>v{record.source_record_version}</span></>
            )}
            <span className={styles.connMetaSep}>·</span>
            <span>{record.observed_at_display || "—"}</span>
          </div>
        </div>
        <div className={styles.idBadges}>
          <span className={record.record_type === "conversation" ? styles.srBadgeConv : styles.srBadgeSys}>{record.record_type}</span>
          <span className={styles.srBadgeLink}>{record.link_status}</span>
          <svg className={`${styles.srcChevron} ${open ? styles.srcChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </div>
      </div>
      {open && (
        <div className={styles.idDetailPanel}>
          <div className={styles.idDetailSection}>
            <div className={styles.idDetailSectionTitle}>Record</div>
            <div className={styles.srMetaGrid}>
              {meta.map(([label, value]) => (
                <div key={label} className={styles.srMetaRow}>
                  <span className={styles.srMetaLabel}>{label}</span>
                  <span className={styles.srMetaValue}>{value}</span>
                </div>
              ))}
            </div>
          </div>

          <div className={styles.idDetailSection}>
            <div className={styles.idDetailSectionTitle}>Normalized payload</div>
            {identifiers.length === 0 && attributes.length === 0 && address === null ? (
              <div className={styles.srMetaValue}>—</div>
            ) : (
              <>
                {identifiers.length > 0 && (
                  <div className={styles.srPills}>
                    {identifiers.map((id, i) => (
                      <span key={`id-${i}`} className={styles.srPill}>
                        {titleCase(id.identifier_type ?? "")} · {id.normalized_value ?? "—"}{id.is_verified ? " ✓" : ""}
                      </span>
                    ))}
                  </div>
                )}
                {address !== null && (
                  <div className={styles.srMetaRow}><span className={styles.srMetaLabel}>Address</span><span className={styles.srMetaValue}>{address}</span></div>
                )}
                {attributes.length > 0 && (
                  <div className={styles.srPills}>
                    {attributes.map((attr, i) => (
                      <span key={`attr-${i}`} className={styles.srPill}>{titleCase(attr.attribute_name ?? "")} · {attr.attribute_value ?? "—"}</span>
                    ))}
                  </div>
                )}
              </>
            )}
          </div>

          {record.chat_transcript !== null && record.chat_transcript.length > 0 && (
            <div className={styles.idDetailSection}>
              <div className={styles.idDetailSectionTitle}>Conversation <span className={styles.srMetaLabel}>· {record.chat_transcript.length} {record.chat_transcript.length === 1 ? "message" : "messages"}</span></div>
              <div className={styles.srChat}>
                {record.chat_transcript.map((m, i) => {
                  const color = speakerColors.get(m.speaker) ?? CHAT_FALLBACK_COLOR;
                  return (
                    <div
                      key={i}
                      className={`${styles.srMsg} ${srChatSide(m.role) === "agent" ? styles.srMsgAgent : styles.srMsgCustomer}`}
                      style={{ borderColor: color, background: `${color}1f` }}
                    >
                      <div className={styles.srMsgHead}>
                        <span className={styles.srMsgSpeaker} style={{ color }}>{m.speaker || "Unknown"}</span>
                        {m.phone !== null && <span className={styles.srMsgPhone}>{m.phone}</span>}
                        {m.role !== null && <span className={styles.srMsgRole}>{titleCase(m.role)}</span>}
                        <span className={styles.srMsgTime}>{m.timestamp_display || m.timestamp}</span>
                      </div>
                      <div className={styles.srMsgText}>{m.text}</div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {record.raw_payload !== null && (
            <div className={styles.idDetailSection}>
              <button type="button" className={styles.srRawToggle} onClick={() => setRawOpen((v) => !v)} aria-expanded={rawOpen}>
                <svg className={`${styles.srcChevron} ${rawOpen ? styles.srcChevronOpen : ""}`} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
                Raw payload <span className={styles.srMetaLabel}>(original source JSON)</span>
              </button>
              {rawOpen && <pre className={styles.srJson}>{JSON.stringify(record.raw_payload, null, 2)}</pre>}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** List + pagination for one entity filter. Owns the paginated hook so the
 *  parent can remount it (key=activeEntity) to reset pagination on filter change. */
function SourceRecordsList({ basePath }: { basePath: string }): ReactElement {
  const { rows, loading, error, from, to, total, hasPrev, hasNext, goNext, goPrev } =
    usePaginatedFetch<PersonSourceRecord>(basePath);
  const records = rows ?? [];

  if (loading) return <SkeletonRows />;
  if (error !== null) return <div className={styles.tabError}>{error}</div>;
  if (records.length === 0) return <TabEmptyState message="No source records on file." />;

  return (
    <>
      <div className={styles.idList}>
        {records.map((record) => (
          <SourceRecordRow key={record.source_record_pk} record={record} />
        ))}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
    </>
  );
}

function SourceRecordsTab({ personId, facets, onTotalLoaded }: { personId: string; facets: SourceRecordEntityFacet[]; onTotalLoaded: (n: number) => void }): ReactElement {
  const [activeEntity, setActiveEntity] = useState<string | null>(null);
  const facetTotal = facets.reduce((sum, f) => sum + f.count, 0);
  const basePath = activeEntity === null
    ? `/bff/persons/${encodeURIComponent(personId)}/source-records`
    : `/bff/persons/${encodeURIComponent(personId)}/source-records?entity_key=${encodeURIComponent(activeEntity)}`;

  useEffect(() => { onTotalLoaded(facetTotal); }, [facetTotal, onTotalLoaded]);

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Source records</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{facetTotal} {facetTotal === 1 ? "record" : "records"}</span>
      </div>

      {facets.length > 0 && (
        <div className={styles.srFilter}>
          <button type="button" className={`${styles.srChip} ${activeEntity === null ? styles.srChipOn : ""}`} onClick={() => setActiveEntity(null)}>
            All · {facetTotal}
          </button>
          {facets.map((f) => {
            const key = f.entity_key ?? f.source_system;
            return (
              <button
                key={`${f.source_system}:${key}`}
                type="button"
                className={`${styles.srChip} ${activeEntity === f.entity_key && f.entity_key !== null ? styles.srChipOn : ""}`}
                onClick={() => setActiveEntity(f.entity_key)}
                disabled={f.entity_key === null}
                title={f.entity_key === null ? "No entity key — cannot filter" : undefined}
              >
                {srEntityLabel(f)} · {f.count}
              </button>
            );
          })}
        </div>
      )}

      {/* key remounts the list when the filter changes, resetting pagination to page 1 */}
      <SourceRecordsList key={activeEntity ?? "__all__"} basePath={basePath} />
    </section>
  );
}

function IdentifiersTab({ identifiers }: { identifiers: PersonIdentifier[] }): ReactElement {
  const count = identifiers.length;
  const [revealedSet, setRevealedSet] = useState<Set<number>>(new Set());
  const [expandedSet, setExpandedSet] = useState<Set<number>>(new Set());

  function toggleReveal(i: number): void {
    setRevealedSet((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }

  function toggleExpand(i: number): void {
    setExpandedSet((prev) => {
      const next = new Set(prev);
      if (next.has(i)) next.delete(i); else next.add(i);
      return next;
    });
  }

  // Backend already sorts: active DESC, type, value — preserve that order
  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Identifiers</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{count} {count === 1 ? "identifier" : "identifiers"}</span>
      </div>
      {count === 0 ? (
        <TabEmptyState message="No identifiers on record." />
      ) : (
        <div className={styles.idList}>
          {identifiers.map((id, i) => {
            const isSensitive = id.identifier_type === "nric";
            const isRevealed  = revealedSet.has(i);
            const isOpen      = expandedSet.has(i);
            const displayValue = isSensitive && !isRevealed
              ? maskNric(id.normalized_value)
              : id.normalized_value;

            const entityNames = [
              ...new Set(id.entities.map((e) => e.display_name).filter((n): n is string => n !== null)),
            ];
            const metaParts: Array<string> = [
              titleCase(id.identifier_type),
              ...entityNames,
              ...(id.source_system_key !== null ? [id.source_system_key] : []),
              ...(id.last_confirmed_at !== null ? [`Last seen ${fmtDate(id.last_confirmed_at)}`] : []),
            ];

            const hasSrcIds = id.source_record_ids !== null && id.source_record_ids.length > 0;
            const hasDetail = id.entities.length > 0 || id.source_records.length > 0 || hasSrcIds;

            return (
              <div key={i} className={styles.idRow}>
                <div className={styles.idRowMain}>
                  <div className={styles.idIconWrap}>{identifierIcon(id.identifier_type)}</div>
                  <div className={styles.idBody}>
                    <div className={styles.idRevealRow}>
                      <span className={styles.idValue}>{displayValue}</span>
                      {isSensitive && (
                        <button
                          type="button"
                          className={styles.revealBtn}
                          onClick={() => toggleReveal(i)}
                          aria-label={isRevealed ? "Hide NRIC" : "Show NRIC"}
                        >
                          {isRevealed ? (
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                              <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
                              <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
                              <line x1="1" y1="1" x2="23" y2="23"/>
                            </svg>
                          ) : (
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
                              <circle cx="12" cy="12" r="3"/>
                            </svg>
                          )}
                        </button>
                      )}
                    </div>
                    <div className={styles.connMeta}>
                      {metaParts.map((part, j) => (
                        <Fragment key={j}>
                          {j > 0 && <span className={styles.connMetaSep}>·</span>}
                          <span>{part}</span>
                        </Fragment>
                      ))}
                    </div>
                  </div>
                  <div className={styles.idBadges}>
                    <span className={id.is_active ? styles.idBadgeActive : styles.idBadgeInactive}>
                      {id.is_active ? "Active" : "Inactive"}
                    </span>
                    {id.is_verified && <span className={styles.idBadgeVerified}>Verified</span>}
                  </div>
                  {hasDetail && (
                    <button
                      type="button"
                      className={styles.idExpandBtn}
                      onClick={() => toggleExpand(i)}
                      aria-expanded={isOpen}
                      aria-label={isOpen ? "Hide details" : "Show details"}
                    >
                      <svg className={`${styles.idChevron} ${isOpen ? styles.idChevronOpen : ""}`} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                        <path d="M6 9l6 6 6-6" />
                      </svg>
                    </button>
                  )}
                </div>
                {isOpen && (
                  <div className={styles.idDetailPanel}>
                    {id.entities.length > 0 && (
                      <div className={styles.idDetailSection}>
                        <div className={styles.idDetailSectionTitle}>Entities</div>
                        <div className={styles.idEntityList}>
                          {id.entities.map((entity) => (
                            <div key={entity.entity_key} className={styles.idEntityRow}>
                              <div className={styles.idEntityName}>{entity.display_name ?? entity.entity_key}</div>
                              <div className={styles.idEntityMeta}>
                                {[entity.entity_type, entity.country_code].filter(Boolean).map((v) => titleCase(v as string)).join(" · ")}
                              </div>
                              <div className={styles.idEntityRight}>
                                <span className={entity.is_active ? styles.idBadgeActive : styles.idBadgeInactive}>
                                  {entity.is_active ? "Active" : "Inactive"}
                                </span>
                                <span className={styles.idEntityCount}>{entity.source_record_count} {entity.source_record_count === 1 ? "record" : "records"}</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    {id.source_records.length > 0 ? (
                      <div className={styles.idDetailSection}>
                        <div className={styles.idDetailSectionTitle}>Source records ({id.source_records.length})</div>
                        <div className={styles.idSrcRecordList}>
                          {id.source_records.map((sr) => {
                            const payload = sr.normalized_payload;
                            const identifiers = payload?.identifiers ?? [];
                            const address = payload?.address ?? null;
                            const attributes = payload?.attributes ?? [];
                            const hasPayload = identifiers.length > 0 || address !== null || attributes.length > 0;
                            return (
                              <div key={sr.source_record_pk} className={styles.idSrcRecordCard}>
                                <div className={styles.idSrcRecordHeader}>
                                  <span className={`${styles.idSrcRecordId} ${styles.mono}`}>{sr.source_record_id}</span>
                                  <span className={`${styles.idSrcChip} ${styles.idSrcChipSystem}`}>{sr.source_system}</span>
                                  {sr.entity_display_name !== null && (
                                    <span className={styles.idSrcChip}>{sr.entity_display_name}</span>
                                  )}
                                  <span className={`${styles.idSrcChip} ${sr.record_type === "conversation" ? styles.idSrcChipConv : ""}`}>{sr.record_type}</span>
                                  <span className={styles.idSrcChip}>{sr.link_status}</span>
                                </div>
                                <div className={styles.idSrcRecordMeta}>
                                  <div className={styles.idSrcMetaItem}>
                                    <span className={styles.idSrcMetaLabel}>Observed</span>
                                    <span className={styles.idSrcMetaValueStrong}>{fmtDate(sr.observed_at)}</span>
                                  </div>
                                  <div className={styles.idSrcMetaItem}>
                                    <span className={styles.idSrcMetaLabel}>Ingested</span>
                                    <span className={styles.idSrcMetaValue}>{fmtDate(sr.ingested_at)}</span>
                                  </div>
                                  {sr.extraction_confidence !== null && (
                                    <div className={styles.idSrcMetaItem}>
                                      <span className={styles.idSrcMetaLabel}>Confidence</span>
                                      <span className={styles.idSrcMetaValue}>{Math.round(sr.extraction_confidence * 100)}%</span>
                                    </div>
                                  )}
                                  {sr.extraction_method !== null && (
                                    <div className={styles.idSrcMetaItem}>
                                      <span className={styles.idSrcMetaLabel}>Method</span>
                                      <span className={styles.idSrcMetaValue}>{sr.extraction_method}</span>
                                    </div>
                                  )}
                                </div>
                                {hasPayload && (
                                  <div className={styles.idSrcPayloadArea}>
                                    {identifiers.length > 0 && (
                                      <div className={styles.idSrcPayloadSection}>
                                        <span className={styles.idSrcMetaLabel}>Identifiers</span>
                                        <div className={styles.idSrcPayloadChips}>
                                          {identifiers.map((pid, pi) => {
                                            const isMatch = pid.normalized_value === id.normalized_value;
                                            return (
                                              <span key={pi} className={`${isMatch ? styles.idSrcPayloadChipMatch : styles.idSrcPayloadChip} ${styles.mono}`}>
                                                {titleCase(pid.identifier_type ?? "")}:{" "}{pid.normalized_value ?? "—"}
                                                {pid.is_verified === true && <span className={styles.idSrcChipVerified}> ✓</span>}
                                              </span>
                                            );
                                          })}
                                        </div>
                                      </div>
                                    )}
                                    {address !== null && address.normalized_full && (
                                      <div className={styles.idSrcPayloadSection}>
                                        <span className={styles.idSrcMetaLabel}>Address</span>
                                        <span className={styles.idSrcPayloadText}>{address.normalized_full}</span>
                                      </div>
                                    )}
                                    {attributes.length > 0 && (
                                      <div className={styles.idSrcPayloadSection}>
                                        <span className={styles.idSrcMetaLabel}>Attributes</span>
                                        <div className={styles.idSrcPayloadChips}>
                                          {attributes.map((attr, ai) => (
                                            <span key={ai} className={styles.idSrcPayloadChip}>
                                              {titleCase(attr.attribute_name ?? "")}: {attr.attribute_value ?? "—"}
                                            </span>
                                          ))}
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      </div>
                    ) : hasSrcIds && (
                      <div className={styles.idDetailSection}>
                        <div className={styles.idDetailSectionTitle}>Source record IDs</div>
                        <div className={styles.idSrcIdList}>
                          {(id.source_record_ids ?? []).map((srcId) => (
                            <span key={srcId} className={`${styles.idSrcId} ${styles.mono}`}>{srcId}</span>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function ConnRow({ conn }: { conn: PersonConnection }): ReactElement {
  return (
    <Link href={`/persons/${conn.person_id}`} className={styles.sharedRow}>
      <div className={styles.sharedAvatar} style={{ background: avatarColor(conn.preferred_full_name ?? "?") }}>
        {personInitials(conn.preferred_full_name)}
      </div>
      <div className={styles.connBody}>
        <div className={styles.sharedName}>{conn.preferred_full_name ?? conn.person_id}</div>
        <div className={styles.connMetaRow}>
          <ConnMetaLine hops={conn.hops} sharedIdentifiers={conn.shared_identifiers} sharedAddresses={conn.shared_addresses} />
          {conn.connection_sources?.map((src, j) => (
            <span key={j} className={styles.connSourceChip}>
              {src.entity_display_name !== null && (
                <span className={styles.connSourceEntity}>{src.entity_display_name}</span>
              )}
              <span className={styles.connSourceKey}>{src.source_system_key}</span>
            </span>
          ))}
        </div>
      </div>
      {conn.knows_relationships.length > 0 && (
        <div className={styles.relRowTags}>
          {conn.knows_relationships.map((rel, j) => (
            <span key={j} className={styles.relBadge}>
              {rel.relationship_label ?? rel.relationship_category}
            </span>
          ))}
        </div>
      )}
    </Link>
  );
}

function ConnectionsTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const knowsResult = usePaginatedFetch<PersonConnection>(`/bff/persons/${encodeURIComponent(personId)}/connections?connection_type=knows`);
  const addrResult  = usePaginatedFetch<PersonConnection>(`/bff/persons/${encodeURIComponent(personId)}/connections?connection_type=address`);

  const knows   = knowsResult.rows ?? [];
  const addrs   = addrResult.rows  ?? [];

  useEffect(() => {
    const allDone = !knowsResult.loading && !addrResult.loading;
    if (allDone) {
      onTotalLoaded((knowsResult.total ?? knows.length) + (addrResult.total ?? addrs.length));
    }
  }, [knowsResult.loading, knowsResult.total, addrResult.loading, addrResult.total, knows.length, addrs.length, onTotalLoaded]);

  const allLoading = knowsResult.loading && addrResult.loading;
  if (allLoading) return <TabSkelShell title="Connections"><SkeletonConnections /></TabSkelShell>;
  if (knowsResult.error) return <section className={styles.contentCard}><div className={styles.tabError}>{knowsResult.error}</div></section>;

  const combinedTotal = (knowsResult.total ?? knows.length) + (addrResult.total ?? addrs.length);

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Connections</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{combinedTotal} {combinedTotal === 1 ? "profile" : "profiles"}</span>
      </div>

      {/* ── Relations — only show while loading or when there are rows ── */}
      {(knowsResult.loading || knows.length > 0) && (
        <div className={styles.connSection}>
          <div className={styles.connSectionLabel}>
            Relations
            <span className={styles.connSectionCount}>{knowsResult.total ?? knows.length}</span>
          </div>
          {knowsResult.loading ? (
            <SkeletonConnections />
          ) : (
            <>
              <div className={styles.sharedList}>
                {knows.map((conn, i) => <ConnRow key={`${conn.person_id}-${i}`} conn={conn} />)}
              </div>
              {(knowsResult.hasPrev || knowsResult.hasNext) && (
                <TabPagination from={knowsResult.from} to={knowsResult.to} total={knowsResult.total} hasPrev={knowsResult.hasPrev} hasNext={knowsResult.hasNext} onPrev={knowsResult.goPrev} onNext={knowsResult.goNext} />
              )}
            </>
          )}
        </div>
      )}

      {/* ── Shared Contacts — only show while loading or when there are rows ── */}
      {(addrResult.loading || addrs.length > 0) && (
        <div className={styles.connSection}>
          <div className={styles.connSectionLabel}>
            Shared Contacts
            <span className={styles.connSectionCount}>{addrResult.total ?? addrs.length}</span>
          </div>
          {addrResult.loading ? (
            <SkeletonConnections />
          ) : (
            <>
              <div className={styles.sharedList}>
                {addrs.map((conn, i) => <ConnRow key={`${conn.person_id}-${i}`} conn={conn} />)}
              </div>
              {(addrResult.hasPrev || addrResult.hasNext) && (
                <TabPagination from={addrResult.from} to={addrResult.to} total={addrResult.total} hasPrev={addrResult.hasPrev} hasNext={addrResult.hasNext} onPrev={addrResult.goPrev} onNext={addrResult.goNext} />
              )}
            </>
          )}
        </div>
      )}

      {/* ── Both empty after loading ── */}
      {!knowsResult.loading && !addrResult.loading && knows.length === 0 && addrs.length === 0 && (
        <TabEmptyState message="No connections found." />
      )}
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
  bankruptcyCases: [],
  sourceRecordFacets: [],
};

const VALID_TABS = new Set<Tab>(["sales", "connections", "identifiers", "source-records", "matches", "timeline", "graph"]);

function parseTabParam(value: string | null): Tab {
  if (value !== null && VALID_TABS.has(value as Tab)) return value as Tab;
  return "sales";
}

// Swallows 404 (optional data not present) but re-throws everything else
// so 401/500 errors surface rather than silently showing zero counts.
function catchNotFound(err: unknown): null {
  if (err instanceof BffError && err.status === 404) return null;
  throw err;
}

export default function PersonDetailPage({ params }: { params: Promise<{ personId: string }> }): ReactElement {
  const { personId } = use(params);
  const searchParams = useSearchParams();

  const [person, setPerson] = useState<Person | null>(null);
  const [detailData, setDetailData] = useState<DetailData>(EMPTY_DETAIL);
  const [tabTotals, setTabTotals] = useState<Partial<Record<Tab, number>>>({});
  const [loading, setLoading] = useState(true);
  const pageLoadId = useId();
  const setGlobalLoading = useSetLoading();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFoundFlag, setNotFoundFlag] = useState(false);
  const [activeTab, setActiveTab] = useState<Tab>(() => parseTabParam(searchParams.get("tab")));
  const [graphOpen, setGraphOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareLoading, setShareLoading] = useState(false);
  const [shareUrl, setShareUrl] = useState<string | null>(null);
  const [shareExpiry, setShareExpiry] = useState<string | null>(null);
  const [shareCopied, setShareCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mergeRedirectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
    if (mergeRedirectTimerRef.current !== null) clearTimeout(mergeRedirectTimerRef.current);
  }, []);
  const [shareError, setShareError] = useState<string | null>(null);

  async function handleShare(): Promise<void> {
    if (shareUrl) { setShareOpen(true); return; }
    setShareLoading(true);
    setShareError(null);
    try {
      const res = await bffFetchEnvelope<PublicLink>(`/bff/persons/${encodeURIComponent(personId)}/public-link`, { method: "POST" });
      const url = `${window.location.origin}${toBasePath(`/public/persons/${res.data.token}`)}`;
      setShareUrl(url);
      setShareExpiry(res.data.expires_at);
      setShareOpen(true);
    } catch (err) {
      setShareError(err instanceof BffError ? err.message : "Failed to generate share link.");
    } finally {
      setShareLoading(false);
    }
  }

  function handleCopy(): void {
    if (!shareUrl) return;
    void navigator.clipboard.writeText(shareUrl)
      .then(() => {
        setShareCopied(true);
        if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
        copyTimerRef.current = setTimeout(() => setShareCopied(false), 2000);
      })
      .catch(() => { setShareError("Failed to copy link. Please copy it manually."); });
  }

  function closeShare(): void {
    setShareOpen(false);
  }

  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergeQuery, setMergeQuery] = useState("");
  const [mergeResults, setMergeResults] = useState<Person[]>([]);
  const [mergeSearching, setMergeSearching] = useState(false);
  const [mergeTarget, setMergeTarget] = useState<MergeTargetProfile | null>(null);
  const [mergeFieldChoices, setMergeFieldChoices] = useState<Record<MergeGoldenField, MergeProfileChoice>>({
    preferred_full_name: "candidate",
    preferred_dob: "candidate",
    preferred_phone: "candidate",
    preferred_email: "candidate",
    preferred_address: "candidate",
    preferred_nric: "candidate",
  });
  const [mergeReason, setMergeReason] = useState("");
  const [mergeSubmitting, setMergeSubmitting] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [mergeSuccessMessage, setMergeSuccessMessage] = useState<string | null>(null);
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

  function buildMergeFields(target: MergeTargetProfile): MergeFieldDraft[] {
    if (person === null) return [];
    return [
      { key: "preferred_full_name", label: "Name", thisRaw: person.preferred_full_name, thisDisplay: person.preferred_full_name, candidateRaw: target.preferred_full_name, candidateDisplay: target.preferred_full_name },
      { key: "preferred_phone", label: "Phone", thisRaw: person.preferred_phone, thisDisplay: person.preferred_phone, candidateRaw: target.preferred_phone, candidateDisplay: target.preferred_phone },
      { key: "preferred_email", label: "Email", thisRaw: person.preferred_email, thisDisplay: person.preferred_email, candidateRaw: target.preferred_email, candidateDisplay: target.preferred_email },
      { key: "preferred_dob", label: "DOB", thisRaw: person.preferred_dob, thisDisplay: person.preferred_dob ? fmtDate(person.preferred_dob) : null, candidateRaw: target.preferred_dob, candidateDisplay: target.preferred_dob ? fmtDate(target.preferred_dob) : null },
      { key: "preferred_nric", label: "NRIC", thisRaw: person.preferred_nric, thisDisplay: person.preferred_nric ? maskNric(person.preferred_nric) : null, candidateRaw: target.preferred_nric, candidateDisplay: target.preferred_nric ? maskNric(target.preferred_nric) : null },
    ];
  }

  function chooseMergeTarget(target: MergeTargetProfile): void {
    const nextChoices: Record<MergeGoldenField, MergeProfileChoice> = {
      preferred_full_name: "candidate",
      preferred_dob: "candidate",
      preferred_phone: "candidate",
      preferred_email: "candidate",
      preferred_address: "candidate",
      preferred_nric: "candidate",
    };
    for (const field of buildMergeFields(target)) {
      nextChoices[field.key] = field.candidateRaw !== null ? "candidate" : "this";
    }
    setMergeTarget(target);
    setMergeFieldChoices(nextChoices);
  }

  function setMergeFieldChoice(field: MergeGoldenField, choice: MergeProfileChoice): void {
    setMergeFieldChoices((current) => ({ ...current, [field]: choice }));
  }

  function buildGoldenProfileSelections(target: MergeTargetProfile): GoldenProfileSelectionRequestBody[] {
    return buildMergeFields(target).flatMap((field): GoldenProfileSelectionRequestBody[] => {
      const choice = mergeFieldChoices[field.key];
      if (choice === "candidate") return [];
      if (field.thisRaw === null) return [];
      if (field.key === "preferred_phone") {
        return [{ field_name: field.key, source_kind: "identifier", selected_value: field.thisRaw, source_record_pk: null, identifier_type: "phone" }];
      }
      if (field.key === "preferred_email") {
        return [{ field_name: field.key, source_kind: "identifier", selected_value: field.thisRaw, source_record_pk: null, identifier_type: "email" }];
      }
      if (field.key === "preferred_nric") {
        return [{ field_name: field.key, source_kind: "identifier", selected_value: field.thisRaw, source_record_pk: null, identifier_type: "nric" }];
      }
      return [];
    });
  }

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
        golden_profile_selections: buildGoldenProfileSelections(mergeTarget),
      };
      const res = await bffFetchEnvelope<ManualMergeResponseBody>("/bff/persons/manual-merge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.data.status === "merged" || res.data.status === "completed") {
        setMergeSuccess(true);
        setMergeSuccessMessage(`Merged successfully. Opening ${mergeTarget.preferred_full_name ?? "survivor profile"}…`);
        mergeRedirectTimerRef.current = setTimeout(() => window.location.replace(toBasePath(`/persons/${mergeTarget.person_id}`)), 900);
        return;
      }
      setMergeError(`Merge request returned status: ${res.data.status}.`);
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
    setMergeFieldChoices({
      preferred_full_name: "candidate",
      preferred_dob: "candidate",
      preferred_phone: "candidate",
      preferred_email: "candidate",
      preferred_address: "candidate",
      preferred_nric: "candidate",
    });
    setMergeReason("");
    setMergeError(null);
    setMergeSuccessMessage(null);
    setMergeSuccess(false);
  }

  function openMergeWithCandidate(candidate: PersonSharedIdentifierCandidate): void {
    chooseMergeTarget({
      person_id: candidate.person_id,
      preferred_full_name: candidate.preferred_full_name,
      preferred_phone: candidate.preferred_phone,
      preferred_email: candidate.preferred_email,
      preferred_dob: candidate.preferred_dob,
      preferred_address: null,
      preferred_nric: null,
    });
    setMergeQuery("");
    setMergeResults([]);
    setMergeReason("");
    setMergeError(null);
    setMergeSuccessMessage(null);
    setMergeSuccess(false);
    setMergeOpen(true);
  }

  const [overrideOpen, setOverrideOpen] = useState(false);
  const [overrideField, setOverrideField] = useState<EditableField>("full_name");
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
      setOverrideError(e instanceof BffError ? e.message : "Failed to save changes.");
    } finally {
      setOverrideSubmitting(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    setGlobalLoading(pageLoadId, true);

    async function load(): Promise<void> {
      try {
        const p = await bffFetch<Person>(`/bff/persons/${encodeURIComponent(personId)}`);
        if (cancelled) return;
        setPerson(p);

        const [idEnv, srcEnv, salesEnv, auditEnv, matchesEnv, connsEnv, bkEnv, sharedEnv, facetsEnv] = await Promise.all([
          bffFetchEnvelope<PersonIdentifier[]>(`/bff/persons/${encodeURIComponent(personId)}/identifiers`).catch(catchNotFound),
          bffFetchEnvelope<PersonSourceRecord[]>(`/bff/persons/${encodeURIComponent(personId)}/source-records`).catch(catchNotFound),
          bffFetchEnvelope<SalesOrder[]>(`/bff/persons/${encodeURIComponent(personId)}/sales`).catch(catchNotFound),
          bffFetchEnvelope<PersonAuditEvent[]>(`/bff/persons/${encodeURIComponent(personId)}/audit`).catch(catchNotFound),
          bffFetchEnvelope<unknown[]>(`/bff/persons/${encodeURIComponent(personId)}/matches?limit=1`).catch(catchNotFound),
          bffFetchEnvelope<unknown[]>(`/bff/persons/${encodeURIComponent(personId)}/connections?limit=1`).catch(catchNotFound),
          bffFetchEnvelope<PersonBankruptcyCase[]>(`/bff/persons/${encodeURIComponent(personId)}/bankruptcy-cases`).catch(catchNotFound),
          bffFetchEnvelope<unknown[]>(`/bff/persons/${encodeURIComponent(personId)}/shared-identifiers?limit=1`).catch(catchNotFound),
          bffFetchEnvelope<SourceRecordEntityFacet[]>(`/bff/persons/${encodeURIComponent(personId)}/source-record-entities`).catch(catchNotFound),
        ]);

        if (cancelled) return;
        setDetailData({
          identifiers: idEnv?.data ?? [],
          sourceRecords: srcEnv?.data ?? [],
          sales: salesEnv?.data ?? [],
          audit: auditEnv?.data ?? [],
          bankruptcyCases: bkEnv?.data ?? [],
          sourceRecordFacets: facetsEnv?.data ?? [],
        });
        const decisionsCount = matchesEnv?.meta.total_count ?? 0;
        const candidatesCount = sharedEnv?.meta.total_count ?? 0;
        setTabTotals({
          sales:       salesEnv?.meta.total_count ?? undefined,
          matches:     (decisionsCount + candidatesCount) || undefined,
          connections: connsEnv?.meta.total_count  ?? undefined,
        });
      } catch (err) {
        if (cancelled) return;
        if (err instanceof BffError && err.status === 404) {
          setNotFoundFlag(true);
        } else {
          setLoadError(err instanceof BffError ? err.message : "Failed to load person.");
        }
      } finally {
        if (!cancelled) { setLoading(false); setGlobalLoading(pageLoadId, false); }
      }
    }

    void load();
    return () => { cancelled = true; setGlobalLoading(pageLoadId, false); };
  }, [personId, pageLoadId, setGlobalLoading]);

  const onMatchesTotal     = useCallback((n: number) => { setTabTotals((p) => ({ ...p, matches:     n })); }, []);
  const onConnectionsTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, connections: n })); }, []);
  const onSalesTotal       = useCallback((n: number) => { setTabTotals((p) => ({ ...p, sales:       n })); }, []);
  const onSourceRecordsTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, "source-records": n })); }, []);

  if (notFoundFlag) notFound();

  if (loading || person === null) {
    return <PersonDetailSkeleton />;
  }

  if (loadError !== null) {
    return <div style={{ padding: "2rem", color: "var(--text-muted)", fontSize: 14 }}>{loadError}</div>;
  }

  const tabs: TabConfig[] = [
    { id: "sales",       label: "Sales",       count: tabTotals.sales       ?? detailData.sales.length },
    { id: "connections", label: "Connections", count: tabTotals.connections ?? person.connection_count },
    { id: "identifiers", label: "Identifiers", count: detailData.identifiers.length || undefined },
    { id: "source-records", label: "Source records", count: tabTotals["source-records"] ?? (detailData.sourceRecordFacets.reduce((sum, f) => sum + f.count, 0) || undefined) },
    { id: "matches",     label: "Matches",     count: tabTotals.matches },
    { id: "timeline",    label: "Timeline" },
    { id: "graph",       label: "Graph" },
  ];

  const shell = (children: ReactElement): ReactElement => (
    <DetailShell person={person} detailData={detailData} salesTotal={tabTotals.sales} tabs={tabs} activeTab={activeTab} onTabChange={setActiveTab} onOverride={() => setOverrideOpen(true)}>
      {children}
    </DetailShell>
  );

  return (
    <div className={styles.page}>
      <PersonBreadcrumb personName={person.preferred_full_name} onShare={() => void handleShare()} shareLoading={shareLoading} />
      <div className={styles.tabContent}>
        {activeTab === "sales"        && shell(<SalesTab personId={personId} onTotalLoaded={onSalesTotal} />)}
        {activeTab === "connections"  && shell(<ConnectionsTab personId={personId} onTotalLoaded={onConnectionsTotal} />)}
        {activeTab === "identifiers"  && shell(<IdentifiersTab identifiers={detailData.identifiers} />)}
        {activeTab === "source-records" && shell(<SourceRecordsTab personId={personId} facets={detailData.sourceRecordFacets} onTotalLoaded={onSourceRecordsTotal} />)}
        {activeTab === "matches"      && shell(<MatchesTab personId={personId} onTotalLoaded={onMatchesTotal} onMergeWith={openMergeWithCandidate} />)}
        {activeTab === "timeline"    && shell(<TimelineTab person={person} detailData={detailData} personId={personId} />)}
        {activeTab === "graph" && (
          <DetailShell person={person} detailData={detailData} salesTotal={tabTotals.sales} tabs={tabs} activeTab={activeTab} onTabChange={setActiveTab} onOverride={() => setOverrideOpen(true)}>
            <div style={{ height: "max(calc(100vh - 340px), 480px)", border: "1px solid var(--border)", borderRadius: "10px", overflow: "hidden" }}>
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
          <div className={`${styles.overrideModal} ${styles.mergeModal}`} onClick={(e) => e.stopPropagation()}>
            {mergeSubmitting && <MergeLoadingOverlay />}
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Merge duplicate profiles</span>
              <button type="button" className={styles.shareModalClose} onClick={closeMerge} disabled={mergeSubmitting || mergeSuccess} aria-label="Close">×</button>
            </div>

            {mergeSuccess ? (
              <div className={styles.mergeSuccess}>
                {mergeSuccessMessage ?? "Merged successfully. Redirecting…"}
              </div>
            ) : (
              <>
                <div className={styles.mergeWarning}>
                  ⚠ This cannot be undone. Both profiles will be combined into one. The "after merge" profile will include data from both.
                </div>

                {/* Search — hidden when target already pre-selected from candidate row */}
                {mergeTarget === null && (
                  <div className={styles.overrideFieldGroup}>
                    <div className={styles.overrideLabel}>Search target profile</div>
                    <input
                      className={styles.mergeSearchInput}
                      type="text"
                      placeholder="Name, phone, email…"
                      value={mergeQuery}
                      onChange={(e) => { setMergeQuery(e.target.value); }}
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
                            className={styles.overrideSrRow}
                            onClick={() => chooseMergeTarget(p)}
                            role="button"
                            tabIndex={0}
                            onKeyDown={(e) => e.key === "Enter" && chooseMergeTarget(p)}
                          >
                            <div className={styles.overrideSrInfo}>
                              <span className={styles.overrideSrId}>{p.preferred_full_name ?? p.person_id}</span>
                              <span className={styles.overrideSrMeta}>
                                {[p.preferred_phone, p.preferred_email].filter(Boolean).join(" · ") || p.person_id}
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {/* Profile + field comparison: this profile | candidate | after merge */}
                {mergeTarget !== null && (() => {
                  const fields = buildMergeFields(mergeTarget).filter((field) => field.thisRaw !== null || field.candidateRaw !== null);
                  return (
                    <div className={styles.mergeCompare}>
                      <div className={styles.mergeFieldHeader}>
                        <span />
                        <span className={styles.mergeFieldHeaderRole}>This profile</span>
                        <span className={styles.mergeFieldHeaderRole}>Candidate</span>
                        <span className={`${styles.mergeFieldHeaderRole} ${styles.mergeFieldHeaderRoleAfter}`}>After merge</span>
                      </div>
                      {fields.length > 0 && (
                        <div className={styles.mergeFieldList}>
                          {fields.map((field) => {
                            const selectedChoice = mergeFieldChoices[field.key];
                            const afterDisplay = selectedChoice === "this" ? field.thisDisplay : field.candidateDisplay;
                            return (
                              <div key={field.key} className={styles.mergeFieldRow}>
                                <span className={styles.mergeFieldLabel}>{field.label}</span>
                                <label className={styles.mergeFieldOption}>
                                  <input
                                    type="radio"
                                    name={`merge-field-${field.key}`}
                                    checked={selectedChoice === "this"}
                                    disabled={field.thisRaw === null}
                                    onChange={() => setMergeFieldChoice(field.key, "this")}
                                  />
                                  <span className={field.thisDisplay ? styles.mergeFieldVal : styles.mergeFieldEmpty}>{field.thisDisplay ?? "—"}</span>
                                </label>
                                <label className={styles.mergeFieldOption}>
                                  <input
                                    type="radio"
                                    name={`merge-field-${field.key}`}
                                    checked={selectedChoice === "candidate"}
                                    disabled={field.candidateRaw === null}
                                    onChange={() => setMergeFieldChoice(field.key, "candidate")}
                                  />
                                  <span className={field.candidateDisplay ? styles.mergeFieldVal : styles.mergeFieldEmpty}>{field.candidateDisplay ?? "—"}</span>
                                </label>
                                <span className={afterDisplay ? styles.mergeFieldAfter : styles.mergeFieldEmpty}>{afterDisplay ?? "—"}</span>
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                  );
                })()}

                {/* Reason */}
                <div className={styles.overrideFieldGroup}>
                  <div className={styles.overrideLabel}>Reason</div>
                  <textarea
                    className={styles.overrideReason}
                    placeholder="Why are you merging these profiles?"
                    value={mergeReason}
                    onChange={(e) => setMergeReason(e.target.value)}
                    rows={2}
                    autoFocus={mergeTarget !== null}
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
              <span className={styles.shareModalTitle}>Edit field</span>
              <button type="button" className={styles.shareModalClose} onClick={() => setOverrideOpen(false)} aria-label="Close">×</button>
            </div>
            <p className={styles.shareModalDesc}>
              Pin a golden profile field to a value from a specific source record.
            </p>

            {/* Field selector */}
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Field</div>
              <div className={styles.overrideFieldPills}>
                {([
                  { key: "full_name", label: "Full name" },
                  { key: "phone",     label: "Phone" },
                  { key: "email",     label: "Email" },
                  { key: "dob",       label: "Date of birth" },
                  { key: "address",   label: "Address" },
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

            {/* Value selector */}
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Choose value</div>
              <div className={styles.overrideSrList}>
                {(() => {
                  const filtered = detailData.sourceRecords.filter(
                    (sr) => extractSrValue(sr, overrideField) !== "—",
                  );
                  if (filtered.length === 0) {
                    return (
                      <div className={styles.overrideSrEmpty}>
                        No source records have a value for this field.
                      </div>
                    );
                  }
                  return filtered.map((sr) => {
                    const newValue = extractSrValue(sr, overrideField);
                    const sourceMeta = [sr.source_system, sr.entity_display_name].filter(Boolean).join(" · ");
                    return (
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
                          <span className={styles.overrideSrValuePrimary}>{newValue}</span>
                          <span className={styles.overrideSrMeta}>{sourceMeta}</span>
                        </div>
                      </label>
                    );
                  });
                })()}
              </div>
            </div>

            {/* Reason */}
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Reason</div>
              <textarea
                className={styles.overrideReason}
                placeholder="Why are you changing this field?"
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
              {overrideSuccess ? "✓ Saved" : overrideSubmitting ? "Saving…" : "Save changes"}
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

      {shareError !== null && (
        <div style={{ padding: "0.5rem 1rem", color: "var(--color-error, #ef4444)", fontSize: 13 }}>{shareError}</div>
      )}

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
