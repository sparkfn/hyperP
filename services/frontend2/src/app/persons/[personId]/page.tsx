"use client";

import { Fragment, use, useCallback, useEffect, useId, useMemo, useRef, useState, type ReactElement } from "react";
import Link from "next/link";
import { notFound } from "next/navigation";
import type { Person, PersonConnection, SalesOrder } from "@/lib/api-types";
import type {
  ChatMessage,
  EditableFieldOptions,
  FieldOption,
  GoldenFieldName,
  GoldenProfileSelectionRequestBody,
  ManualMergeRequestBody,
  ManualMergeResponseBody,
  UnmergeRequestBody,
  UnmergeResponseBody,
  PersonAuditEvent,
  PersonBankruptcyCase,
  PersonFieldOptions,
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
import ReviewActionsPanel from "@/components/ReviewActionsPanel";
import { ReviewCaseDetailModal } from "@/app/review/[reviewCaseId]/page";
import type { ReviewCaseDetail, ReviewCaseSummary } from "@/lib/api-types-ops";
import styles from "./person.module.css";

type Tab = "sales" | "connections" | "identifiers" | "decision-history" | "source-records" | "matches" | "timeline" | "graph";

type DetailData = {
  identifiers: PersonIdentifier[];
  sourceRecords: PersonSourceRecord[];
  sales: SalesOrder[];
  audit: PersonAuditEvent[];
  bankruptcyCases: PersonBankruptcyCase[];
  sourceRecordFacets: SourceRecordEntityFacet[];
};


type SectionConfig = { id: string; label: string; count?: number };

interface BentoSectionProps {
  section: SectionConfig;
  className?: string;
  highlighted: boolean;
  action?: ReactElement;
  children: ReactElement;
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
  matchSourceLabel?: string;
  matchEvidence?: string;
  currentEvidence?: string;
  candidateEvidence?: string;
}

function validateCustomOverrideValue(fieldName: GoldenFieldName, value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return "Custom value cannot be empty.";
  if (fieldName === "preferred_phone" && !/^\+?[0-9][0-9\s-]{5,19}$/.test(trimmed)) return "Phone must contain only numbers, spaces, hyphens, and an optional leading +.";
  if (fieldName === "preferred_email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmed)) return "Email must be a valid email address.";
  if (fieldName === "preferred_dob" && (!/^\d{4}-\d{2}-\d{2}$/.test(trimmed) || Number.isNaN(Date.parse(trimmed)))) return "Date of birth must use YYYY-MM-DD.";
  if (fieldName === "preferred_nric" && !/^[A-Za-z0-9*\-\s]{4,32}$/.test(trimmed)) return "NRIC must contain only letters, numbers, spaces, hyphens, or *.";
  return null;
}

function customOverrideInputType(fieldName: GoldenFieldName): string {
  if (fieldName === "preferred_email") return "email";
  if (fieldName === "preferred_dob") return "date";
  if (fieldName === "preferred_phone") return "tel";
  return "text";
}

function customOverrideInputMode(fieldName: GoldenFieldName): "text" | "email" | "tel" | undefined {
  if (fieldName === "preferred_email") return "email";
  if (fieldName === "preferred_phone") return "tel";
  return undefined;
}

function customOverridePlaceholder(fieldName: GoldenFieldName): string {
  if (fieldName === "preferred_full_name") return "Type full name";
  if (fieldName === "preferred_phone") return "+6581234567";
  if (fieldName === "preferred_email") return "name@example.com";
  if (fieldName === "preferred_dob") return "YYYY-MM-DD";
  if (fieldName === "preferred_nric") return "Type NRIC";
  return "Type address";
}

type OverrideFieldSelection =
  | { field: EditableFieldOptions; sourceRecordPk: string }
  | { field: EditableFieldOptions; customValue: string };

interface MergeFieldDraft {
  key: MergeGoldenField;
  label: string;
  thisRaw: string | null;
  thisDisplay: string | null;
  candidateRaw: string | null;
  candidateDisplay: string | null;
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

function PersonSidebar({ person, detailData, personId, onOverride, onGraphOpen }: { person: Person; detailData: DetailData; personId: string; onOverride: () => void; onGraphOpen: () => void }): ReactElement {
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
            <button type="button" className={styles.editFieldBtn} onClick={onOverride}>
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
              </svg>
              <span>Edit field</span>
            </button>
          </div>
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

        <div className={styles.profileHeroCompleteness} aria-label={`Profile completeness ${completeness}%`}>
          <div className={styles.profileHeroCompletenessMeta}>
            <span className={styles.profileHeroCompletenessLabel}>Profile filled</span>
            <span className={styles.profileHeroCompPct}>{completeness}%</span>
          </div>
          <div className={styles.profileHeroBar} aria-hidden="true">
            <span style={{ width: `${completeness}%` }} />
          </div>
        </div>

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
      <section className={styles.sidebarCard}>
        <div className={`${styles.sourceEntityHeader} ${styles.sidebarGraphHeader}`}>
          <span className={styles.bkSectionLabel}>Graph</span>
          <button type="button" className={styles.sidebarGraphExpand} onClick={onGraphOpen} aria-label="Expand graph">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M15 3h6v6" />
              <path d="M10 14 21 3" />
              <path d="M9 21H3v-6" />
              <path d="M14 10 3 21" />
            </svg>
          </button>
        </div>
        <div className={styles.sidebarGraphBody}>
          <PersonFocusedGraph
            initialPersonId={person.person_id}
            initialTitle={person.preferred_full_name ?? person.person_id}
            overlayMode
            pureGraph
            onMaximize={onGraphOpen}
          />
        </div>
      </section>
      <section className={styles.sidebarCard}>
        <div className={styles.sourceEntityHeader}>
          <span className={styles.bkSectionLabel}>Timeline</span>
          <span className={styles.sourceEntityBadge}>{buildTimeline(detailData).length} activity</span>
        </div>
        <div className={styles.sidebarTimelineBody}>
          <TimelineTab person={person} detailData={detailData} personId={personId} />
        </div>
      </section>
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

function SectionNav({ sections, scrollRef, onJump }: { sections: SectionConfig[]; scrollRef: { current: HTMLDivElement | null }; onJump: (id: string) => void }): ReactElement {
  const [activeId, setActiveId] = useState(sections[0]?.id ?? "");
  const sectionIdsKey = sections.map((s) => s.id).join(",");

  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const ids = sectionIdsKey.split(",");
    const observers: IntersectionObserver[] = [];
    for (const id of ids) {
      const el = document.getElementById(id);
      if (!el) continue;
      const obs = new IntersectionObserver(
        ([entry]) => { if (entry?.isIntersecting) setActiveId(id); },
        { root, rootMargin: "0px 0px -70% 0px", threshold: 0 },
      );
      obs.observe(el);
      observers.push(obs);
    }
    return () => { observers.forEach((o) => o.disconnect()); };
  }, [sectionIdsKey, scrollRef]);

  function scrollTo(id: string): void {
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
    window.history.replaceState(null, "", `#${id}`);
  }

  return (
    <div className={styles.tabs}>
      {sections.map(({ id, label, count }) => (
        <button
          key={id}
          type="button"
          className={`${styles.tab} ${activeId === id ? styles.tabActive : ""}`}
          onClick={() => { setActiveId(id); scrollTo(id); onJump(id); }}
        >
          <span>{label}</span>
          {count != null && count > 0 && <span className={styles.tabCount}>{count}</span>}
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

function RightRail({ person, detailData, salesTotal, sections, scrollRef, onSectionJump, children }: { person: Person; detailData: DetailData; salesTotal: number | undefined; sections: SectionConfig[]; scrollRef: { current: HTMLDivElement | null }; onSectionJump: (id: string) => void; children: ReactElement }): ReactElement {
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
      <div className={styles.rightTabsInline}>
        <SectionNav sections={sections} scrollRef={scrollRef} onJump={onSectionJump} />
      </div>
      <div className={styles.tabPanelScroll} ref={(el) => { scrollRef.current = el; }}>{children}</div>
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

function DetailShell({ person, detailData, personId, salesTotal, children, sections, scrollRef, onSectionJump, onOverride, onGraphOpen }: { person: Person; detailData: DetailData; personId: string; salesTotal: number | undefined; children: ReactElement; sections: SectionConfig[]; scrollRef: { current: HTMLDivElement | null }; onSectionJump: (id: string) => void; onOverride: () => void; onGraphOpen: () => void }): ReactElement {
  return (
    <div className={styles.detailLayout}>
      <PersonSidebar person={person} detailData={detailData} personId={personId} onOverride={onOverride} onGraphOpen={onGraphOpen} />
      <RightRail person={person} detailData={detailData} salesTotal={salesTotal} sections={sections} scrollRef={scrollRef} onSectionJump={onSectionJump}>
        {children}
      </RightRail>
    </div>
  );
}

function BentoSection({ section, className, highlighted, action, children }: BentoSectionProps): ReactElement {
  const sectionClassName = `${styles.collapsibleSection}${className ? ` ${className}` : ""}${highlighted ? ` ${styles.collapsibleSectionHighlight}` : ""}`;

  return (
    <section id={section.id} className={sectionClassName}>
      <div className={styles.collapsibleHeader}>
        <span className={styles.collapsibleTitleWrap}>
          <span className={styles.collapsibleTitle}>{section.label}</span>
          {typeof section.count === "number" && <span className={styles.collapsibleCount}>{section.count}</span>}
        </span>
        {action}
      </div>
      <div className={styles.collapsibleBody}>
        {children}
      </div>
    </section>
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

function MatchEmptyState({ message }: { message: string }): ReactElement {
  return <p className={styles.matchEmptyTextOnly}>{message}</p>;
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
          <div className={styles.tabSkelStack} style={{ flex: 1 }}>
            <span className={styles.tabSkelLine} style={{ width: `${55 + (i % 3) * 12}%` }} />
            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
              <span className={styles.tabSkelLine} style={{ width: `${35 + (i % 4) * 10}%`, height: 10 }} />
              <span className={styles.tabSkelLine} style={{ width: 24, height: 10 }} />
            </div>
          </div>
          <span className={styles.tabSkelBadge} style={{ width: 64 }} />
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
          <span className={styles.tabSkelAvatar} />
          <div className={styles.tabSkelStack} style={{ flex: 1 }}>
            <span className={styles.tabSkelLine} style={{ width: `${50 + (i % 3) * 15}%` }} />
            <span className={styles.tabSkelLine} style={{ width: `${40 + (i % 2) * 20}%`, height: 10 }} />
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
          <span className={styles.tabSkelAvatar} />
          <div className={styles.tabSkelStack} style={{ flex: 1 }}>
            <span className={styles.tabSkelLine} style={{ width: `${55 + (i % 3) * 12}%` }} />
            <span className={styles.tabSkelLine} style={{ width: `${35 + (i % 2) * 15}%`, height: 10 }} />
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            <span className={styles.tabSkelBadge} style={{ width: 48 }} />
          </div>
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

function sourceRecordNameEvidence(record: PersonSourceRecord): string {
  const nameAttribute = record.normalized_payload?.attributes?.find((attribute) =>
    attribute.attribute_name?.toLowerCase().includes("name"),
  );
  return nameAttribute?.quality_flag ?? titleCase(record.record_type);
}

function CandidateSourceRecords({ title, records, group }: { title: string; records: PersonSourceRecord[]; group: SharedIdentifierGroup }): ReactElement {
  return (
    <div className={styles.matchSourceColumn}>
      <span className={styles.matchSourceTitle}>{title}</span>
      <span className={styles.matchSourceMeta}>{group.normalized_value}</span>
      {records.length === 0 ? (
        <span className={styles.matchSourceEmpty}>No source record shown</span>
      ) : records.map((record) => (
        <span key={record.source_record_pk} className={styles.matchSourceSub}>
          {sourceRecordMeta(record)} · {sourceRecordEvidence(record, group)} · observed {fmtDate(record.observed_at)}
        </span>
      ))}
    </div>
  );
}

function sourceSummary(record: PersonSourceRecord, group: SharedIdentifierGroup | undefined): string {
  const evidence = group ? sourceRecordEvidence(record, group) : sourceRecordNameEvidence(record);
  return `${sourceRecordMeta(record)} · ${evidence} · observed ${fmtDate(record.observed_at)}`;
}

function sourceSummaries(records: PersonSourceRecord[], group: SharedIdentifierGroup | undefined): string[] {
  return records.map((record) => sourceSummary(record, group));
}

function RecommendedEvidenceCard({
  signals,
  currentSources,
  candidateSources,
}: {
  signals: { label: string; currentValue: string; candidateValue: string }[];
  currentSources: string[];
  candidateSources: string[];
}): ReactElement {
  return (
    <div className={styles.recommendedEvidenceBlock}>
      <div className={styles.recommendedEvidenceTable}>
        <div className={styles.recommendedEvidenceHeader} />
        <div className={styles.recommendedEvidenceHeader}>Current profile</div>
        <div className={styles.recommendedEvidenceHeader}>Candidate evidence</div>
        {signals.map((signal) => (
          <Fragment key={signal.label}>
            <div className={styles.recommendedEvidenceSignal}>{signal.label}</div>
            <div className={styles.recommendedEvidenceCellValue}>{signal.currentValue}</div>
            <div className={styles.recommendedEvidenceCellValue}>{signal.candidateValue}</div>
          </Fragment>
        ))}
        <div className={styles.recommendedEvidenceSignal}>Source</div>
        <div className={styles.recommendedEvidenceSourceStack}>
          {(currentSources.length > 0 ? currentSources : ["No source record shown"]).map((source, i) => (
            <span key={`current-${source}-${i}`}>{source}</span>
          ))}
        </div>
        <div className={styles.recommendedEvidenceSourceStack}>
          {(candidateSources.length > 0 ? candidateSources : ["No source record shown"]).map((source, i) => (
            <span key={`candidate-${source}-${i}`}>{source}</span>
          ))}
        </div>
      </div>
    </div>
  );
}

function CandidateDetailPanel({ detail }: { detail: PossibleMatchDetail }): ReactElement {
  return (
    <div className={styles.candidateDetailPanel}>
      <div className={styles.candidateReasonList}>
        {detail.shared_identifier_groups.map((group) => (
          <div key={`${group.identifier_type}-${group.normalized_value}`} className={styles.candidateReasonCard}>
            <div className={styles.candidateReasonTop}>
              <span className={styles.candidateReasonLabel}>Shared {titleCase(group.identifier_type)}</span>
              <span className={`${styles.candidateReasonValue} ${styles.mono}`}>{group.normalized_value}</span>
            </div>
            <div className={styles.candidateReasonSummary}>The same {titleCase(group.identifier_type).toLowerCase()} appears in source records for both profiles.</div>
            <div className={styles.matchSourceGrid}>
              <CandidateSourceRecords title="Current profile" records={group.current_person_source_records} group={group} />
              <CandidateSourceRecords title="Candidate evidence" records={group.candidate_source_records} group={group} />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function recommendationEvidenceLabel(reason: string): string {
  const lower = reason.toLowerCase();
  if (lower.includes("government") || lower.includes("nric") || lower.includes("id hash")) return "Government ID";
  if (lower.includes("phone")) return "Phone signal";
  if (lower.includes("email")) return "Email signal";
  if (lower.includes("name")) return "Name similarity";
  if (lower.includes("address")) return "Address signal";
  if (lower.includes("dob") || lower.includes("birth")) return "DOB signal";
  return "Match signal";
}

function reasonToShortLabel(reason: string): string {
  const lower = reason.toLowerCase();
  if (lower.includes("government") || lower.includes("nric") || lower.includes("id hash")) return "Govt ID match";
  if (lower.includes("phone")) return "Same phone";
  if (lower.includes("email")) return "Same email";
  if (lower.includes("name")) return "Similar name";
  if (lower.includes("address")) return "Same address";
  if (lower.includes("dob") || lower.includes("birth")) return "Similar DOB";
  return "Match signal";
}

function recommendedOtherPersonId(match: PersonMatchDecision, currentPersonId: string): string | null {
  if (match.left_person_id === currentPersonId) return match.right_person_id;
  if (match.right_person_id === currentPersonId) return match.left_person_id;
  return match.right_person_id ?? match.left_person_id;
}

function governmentIdentifierValue(identifiers: PersonIdentifier[] | undefined): string {
  const identifier = identifiers?.find((item) => {
    const type = item.identifier_type.toLowerCase();
    return type.includes("nric") || type.includes("government") || type.includes("govt") || type.includes("id");
  });
  return identifier?.normalized_value ?? "Value unavailable";
}

function recommendationEvidenceValue(reason: string, side: "current" | "candidate", identifiers?: PersonIdentifier[]): string {
  const lower = reason.toLowerCase();
  if (lower.includes("government") || lower.includes("nric") || lower.includes("id hash")) {
    return governmentIdentifierValue(identifiers);
  }
  return reason;
}

function RecommendedMatchDetailPanel({
  match,
  detail,
  error,
  isLoading,
  currentPerson,
  candidatePerson,
  currentIdentifiers,
  candidateIdentifiers,
}: {
  match: PersonMatchDecision;
  detail: PossibleMatchDetail | undefined;
  error: string | undefined;
  isLoading: boolean;
  currentPerson: Person | undefined;
  candidatePerson: Person | undefined;
  currentIdentifiers: PersonIdentifier[] | undefined;
  candidateIdentifiers: PersonIdentifier[] | undefined;
}): ReactElement {
  if (isLoading) return <div className={styles.candidateDetailStatus}>Loading match comparison…</div>;
  const hasNameSignal = match.reasons.some((reason) => reason.toLowerCase().includes("name"));
  const currentNameSource = detail?.shared_identifier_groups.flatMap((group) => group.current_person_source_records)[0];
  const candidateNameSource = detail?.shared_identifier_groups.flatMap((group) => group.candidate_source_records)[0];
  const sharedGroups = detail?.shared_identifier_groups ?? [];
  const firstGroup = sharedGroups[0];
  const combinedSignals = [
    ...sharedGroups.map((group) => ({
      label: `Shared ${titleCase(group.identifier_type)}`,
      currentValue: group.normalized_value,
      candidateValue: group.normalized_value,
    })),
    ...(hasNameSignal ? [{
      label: "Name similarity",
      currentValue: currentPerson?.preferred_full_name ?? "No current name",
      candidateValue: candidatePerson?.preferred_full_name ?? "No candidate name",
    }] : []),
  ];

  return (
    <div className={styles.recommendedUnifiedCard}>
      <div className={styles.recommendedUnifiedHeader}>
        <span className={styles.candidateReasonLabel}>Pair decision</span>
        <span className={`${styles.matchDecisionBadge} ${decisionBadgeClass(match.decision) ?? ""}`}>{titleCase(match.decision)}</span>
        <span className={styles.recommendedConfidence}>{Math.round(match.confidence * 100)}%</span>
      </div>

      {combinedSignals.length > 0 ? (
        <RecommendedEvidenceCard
          signals={combinedSignals}
          currentSources={firstGroup ? sourceSummaries(firstGroup.current_person_source_records, firstGroup) : currentNameSource ? [sourceSummary(currentNameSource, undefined)] : []}
          candidateSources={firstGroup ? sourceSummaries(firstGroup.candidate_source_records, firstGroup) : candidateNameSource ? [sourceSummary(candidateNameSource, undefined)] : []}
        />
      ) : match.reasons.map((reason) => (
        <div key={reason} className={styles.recommendedEvidenceBlock}>
          <div className={styles.candidateReasonTop}>
            <span className={styles.candidateReasonLabel}>{recommendationEvidenceLabel(reason)}</span>
          </div>
          <div className={styles.matchSourceGrid}>
            <div className={styles.matchSourceColumn}>
              <span className={styles.matchSourceTitle}>Current profile</span>
              <span className={styles.matchSourceMeta}>{recommendationEvidenceValue(reason, "current", currentIdentifiers)}</span>
              <span className={styles.matchSourceSub}>{titleCase(match.engine_type)} match engine</span>
            </div>
            <div className={styles.matchSourceColumn}>
              <span className={styles.matchSourceTitle}>Candidate evidence</span>
              <span className={styles.matchSourceMeta}>{recommendationEvidenceValue(reason, "candidate", candidateIdentifiers)}</span>
              <span className={styles.matchSourceSub}>Pair-level match decision</span>
            </div>
          </div>
        </div>
      ))}

      {match.blocking_conflicts.length > 0 && (
        <div className={styles.recommendedSignalPanel}>
          <div className={styles.recommendedSignalGroup}>
            <span className={styles.matchSourceTitle}>Conflicts</span>
            <div className={styles.recommendedConflictList}>
              {match.blocking_conflicts.map((conflict) => <span key={conflict}>{conflict}</span>)}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function RecommendedReviewCaseRow({
  reviewCase,
  currentPerson,
  candidatePerson,
  currentIdentifiers,
  candidateIdentifiers,
  detail,
  reviewCaseDetail,
  error,
  isLoading,
  isOpen,
  onToggle,
  onReview,
  onView,
  onRecreate,
  onRecreateAndUnmerge,
  needsUnmergeBeforeReview,
}: {
  reviewCase: ReviewCaseSummary;
  currentPerson: Person | undefined;
  candidatePerson: Person | undefined;
  currentIdentifiers: PersonIdentifier[] | undefined;
  candidateIdentifiers: PersonIdentifier[] | undefined;
  detail: PossibleMatchDetail | undefined;
  reviewCaseDetail: ReviewCaseDetail | undefined;
  error: string | undefined;
  isLoading: boolean;
  isOpen: boolean;
  onToggle: () => void;
  onReview: () => void;
  onView: () => void;
  onRecreate?: () => void;
  onRecreateAndUnmerge: (() => void) | null;
  needsUnmergeBeforeReview: boolean;
}): ReactElement {
  const currentPersonId = currentPerson?.person_id;
  const candidateFallbackName = reviewCase.right_person_id !== currentPersonId
    ? reviewCase.right_person_name
    : reviewCase.left_person_name;
  const name = candidatePerson?.preferred_full_name ?? candidateFallbackName ?? "Recommended match";
  const reasonLabels = Array.from(new Set((reviewCaseDetail?.match_decision.reasons ?? []).map(reasonToShortLabel)));
  const visibleLabels = reasonLabels.slice(0, 3);
  const extraCount = reasonLabels.length - visibleLabels.length;
  const subtitle = visibleLabels.length > 0
    ? `${visibleLabels.join(" · ")}${extraCount > 0 ? ` · +${extraCount} more` : ""}`
    : [titleCase(reviewCase.match_decision.engine_type), `Case ${reviewCase.review_case_id.slice(0, 12)}…`].join(" · ");
  const confidencePct = Math.round(reviewCase.match_decision.confidence * 100);
  return (
    <div className={styles.candidateCard}>
      <div className={styles.candidateRowShell}>
        <div className={styles.candidateRowButton}>
          <div className={styles.sharedAvatar} style={{ background: avatarColor(name) }}>
            {personInitials(name)}
          </div>
          <div className={styles.candidateMain}>
            <div className={styles.candidateNameRow}>
              <span className={styles.candidateName}>{name}</span>
            </div>
            <div className={styles.candidateReason}>{subtitle}</div>
          </div>
        </div>
        <div className={styles.candidateRowActions}>
          <div className={styles.confidenceBlock}>
            <span className={styles.recommendedConfidence}>{confidencePct}%</span>
            <span className={styles.confidenceLabel}>match</span>
          </div>
          {reviewCase.queue_state === "resolved" ? (
            <>
              {onRecreateAndUnmerge !== null ? (
                <button type="button" className={styles.candidateMergeBtn} onClick={onRecreateAndUnmerge}>
                  Recreate
                </button>
              ) : onRecreate !== undefined ? (
                <button type="button" className={styles.candidateMergeBtn} onClick={onRecreate}>
                  Recreate
                </button>
              ) : null}
            </>
          ) : needsUnmergeBeforeReview && onRecreateAndUnmerge !== null ? (
            <button type="button" className={styles.candidateMergeBtn} onClick={onRecreateAndUnmerge}>
              Recreate
            </button>
          ) : (
            <button type="button" className={styles.candidateMergeBtn} onClick={onReview}>
              Review
            </button>
          )}
          <button type="button" className={styles.candidateDirectLink} onClick={onView} aria-label="Open review case detail">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M14 3h7v7" />
              <path d="M10 14L21 3" />
              <path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5" />
            </svg>
          </button>
          <button type="button" className={styles.candidateExpandButton} onClick={onToggle} aria-expanded={isOpen} aria-label="Toggle match detail">
            <svg className={styles.candidateChevron} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
        </div>
      </div>
      {isOpen ? (
        <RecommendedMatchDetailPanel
          match={{
            match_decision_id: reviewCase.match_decision.match_decision_id,
            engine_type: reviewCase.match_decision.engine_type,
            engine_version: "",
            policy_version: "",
            decision: reviewCase.match_decision.decision,
            confidence: reviewCase.match_decision.confidence,
            reasons: reviewCaseDetail?.match_decision.reasons ?? [],
            blocking_conflicts: reviewCaseDetail?.match_decision.blocking_conflicts ?? [],
            created_at: "",
            left_person_id: reviewCase.left_person_id ?? null,
            right_person_id: reviewCase.right_person_id ?? null,
            review_case_id: reviewCase.review_case_id,
            review_case_queue_state: reviewCase.queue_state,
            review_case_assigned_to: reviewCase.assigned_to,
          }}
          detail={detail}
          error={error}
          isLoading={isLoading}
          currentPerson={currentPerson}
          candidatePerson={candidatePerson}
          currentIdentifiers={currentIdentifiers}
          candidateIdentifiers={candidateIdentifiers}
        />
      ) : null}
    </div>
  );
}

function RecommendedMatchRow({
  match,
  currentPersonId,
  currentPerson,
  person,
  currentIdentifiers,
  candidateIdentifiers,
  detail,
  error,
  isLoading,
  isOpen,
  onToggle,
  onReview,
}: {
  match: PersonMatchDecision;
  currentPersonId: string;
  currentPerson: Person | undefined;
  person: Person | undefined;
  currentIdentifiers: PersonIdentifier[] | undefined;
  candidateIdentifiers: PersonIdentifier[] | undefined;
  detail: PossibleMatchDetail | undefined;
  error: string | undefined;
  isLoading: boolean;
  isOpen: boolean;
  onToggle: () => void;
  onReview: () => void;
}): ReactElement {
  const otherPersonId = recommendedOtherPersonId(match, currentPersonId);
  const confidencePct = Math.round(match.confidence * 100);
  const evidenceLabels = Array.from(new Set(match.reasons.map((reason) => recommendationEvidenceLabel(reason))));
  const rowLabels = evidenceLabels.slice(0, 2);
  const hiddenReasonCount = Math.max(evidenceLabels.length - rowLabels.length, 0);
  const reasonSummary = rowLabels.length > 0
    ? `${rowLabels.join(" · ")}${hiddenReasonCount > 0 ? ` · +${hiddenReasonCount} more` : ""}`
    : "Pair match recommendation";
  const label = person?.preferred_full_name ?? otherPersonId ?? recommendationEvidenceLabel(match.reasons[0] ?? match.decision);

  return (
    <div className={`${styles.candidateCard} ${isOpen ? styles.candidateCardOpen : ""}`}>
      <div className={styles.candidateRowShell}>
        <button type="button" className={styles.candidateRowButton} onClick={onToggle} aria-expanded={isOpen}>
          <div className={styles.sharedAvatar} style={{ background: avatarColor(label) }}>
            {personInitials(label)}
          </div>
          <div className={styles.connBody}>
            <div className={styles.sharedName}>{label}</div>
            <div className={styles.candidateMetaText}>
              <span className={styles.recommendedReasonChip}>{reasonSummary}</span>
            </div>
          </div>
        </button>
        <div className={styles.candidateRowActions}>
          <div className={styles.confidenceBlock}>
            <span className={styles.recommendedConfidence}>{confidencePct}%</span>
            <span className={styles.confidenceLabel}>match</span>
          </div>
          <button type="button" className={styles.candidateMergeBtn} onClick={onReview}>
            Review
          </button>
          {otherPersonId ? (
            <Link href={`/persons/${encodeURIComponent(otherPersonId)}`} className={styles.candidateDirectLink} aria-label="Open recommended match person">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
                <polyline points="15 3 21 3 21 9" />
                <path d="M10 14L21 3" />
              </svg>
            </Link>
          ) : null}
          <button type="button" className={styles.candidateExpandButton} onClick={onToggle} aria-expanded={isOpen} aria-label={isOpen ? "Hide recommended match details" : "Show recommended match details"}>
            <svg className={styles.candidateChevron} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M6 9l6 6 6-6" />
            </svg>
          </button>
        </div>
      </div>
      {isOpen && <RecommendedMatchDetailPanel match={match} detail={detail} error={error} isLoading={isLoading} currentPerson={currentPerson} candidatePerson={person} currentIdentifiers={currentIdentifiers} candidateIdentifiers={candidateIdentifiers} />}
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
            <div className={styles.candidateMetaText}>
              {candidate.identifiers.slice(0, 3).map((id, j) => (
                <span key={j} className={styles.candidateMetaItem}>
                  {titleCase(id.identifier_type)} · {id.normalized_value}
                </span>
              ))}
              {candidate.identifiers.length > 3 && (
                <span className={styles.candidateMetaItem}>+{candidate.identifiers.length - 3} more</span>
              )}
            </div>
          </div>
        </button>
        <div className={styles.candidateRowActions}>
          <Link href={`/persons/${candidate.person_id}`} className={styles.candidateDirectLink} aria-label="Open linked profile" title="Open linked profile">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M14 3h7v7" />
              <path d="M21 3l-9 9" />
              <path d="M10 5H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5" />
            </svg>
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

function MatchesTab({ personId, currentPerson, currentIdentifiers, activeMatchesTab, onTotalLoaded, onMergeWith }: { personId: string; currentPerson: Person | undefined; currentIdentifiers: PersonIdentifier[]; activeMatchesTab: "candidates" | "resolved-cases" | "merge-history"; onTotalLoaded: (n: number) => void; onMergeWith: (candidate: PersonSharedIdentifierCandidate, detail: PossibleMatchDetail | undefined) => void }): ReactElement {
  const [expandedCandidate, setExpandedCandidate] = useState<string | null>(null);
  const [expandedRecommendedMatch, setExpandedRecommendedMatch] = useState<string | null>(null);
  const [recommendedPeople, setRecommendedPeople] = useState<Record<string, Person>>({});
  const [recommendedIdentifiers, setRecommendedIdentifiers] = useState<Record<string, PersonIdentifier[]>>({});
  const [recommendedDetails, setRecommendedDetails] = useState<Record<string, PossibleMatchDetail>>({});
  const [recommendedErrors, setRecommendedErrors] = useState<Record<string, string>>({});
  const [recommendedReviewCases, setRecommendedReviewCases] = useState<ReviewCaseSummary[]>([]);
  const [recommendedReviewLoading, setRecommendedReviewLoading] = useState<boolean>(true);
  const [recommendedReviewError, setRecommendedReviewError] = useState<string | null>(null);
  const [recommendedActionError, setRecommendedActionError] = useState<string | null>(null);
  const [openReviewDecisionIds, setOpenReviewDecisionIds] = useState<Set<string>>(new Set());
  const showResolvedReviewCases = activeMatchesTab === "resolved-cases";
  const [reviewActionCase, setReviewActionCase] = useState<ReviewCaseSummary | null>(null);
  const [reviewCaseIdsByDecision, setReviewCaseIdsByDecision] = useState<Record<string, string>>({});
  const [reviewCaseDetails, setReviewCaseDetails] = useState<Record<string, ReviewCaseDetail>>({});
  const [reviewActionMatch, setReviewActionMatch] = useState<PersonMatchDecision | null>(null);
  const [candidateDetails, setCandidateDetails] = useState<Record<string, PossibleMatchDetail>>({});
  const [candidateErrors, setCandidateErrors] = useState<Record<string, string>>({});
  const [loadingCandidateId, setLoadingCandidateId] = useState<string | null>(null);
  const [unmergeTarget, setUnmergeTarget] = useState<PersonAuditEvent | null>(null);
  const [unmergeReason, setUnmergeReason] = useState("");
  const [unmergeSubmitting, setUnmergeSubmitting] = useState(false);
  const [unmergeError, setUnmergeError] = useState<string | null>(null);
  const [viewingReviewCaseId, setViewingReviewCaseId] = useState<string | null>(null);

  const recommendedResult = usePaginatedFetch<PersonMatchDecision>(
    `/bff/persons/${encodeURIComponent(personId)}/matches`,
  );
  const mergeHistoryResult = usePaginatedFetch<PersonAuditEvent>(
    `/bff/persons/${encodeURIComponent(personId)}/audit`,
  );

  const reloadRecommendedReviewCases = useCallback((): (() => void) => {
    let ignore = false;
    setRecommendedReviewLoading(true);
    setRecommendedReviewError(null);
    bffFetchEnvelope<ReviewCaseSummary[]>(
      `/bff/review-cases?person_id=${encodeURIComponent(personId)}&resolved=${showResolvedReviewCases ? "true" : "false"}&sort_by=created_at&sort_order=DESC&limit=100`,
    ).then((response) => {
      if (ignore) return;
      const filteredCases = response.data.filter(
        (reviewCase) => reviewCase.left_person_id !== undefined && reviewCase.left_person_id !== null
          && reviewCase.right_person_id !== undefined && reviewCase.right_person_id !== null,
      );
      const seenDecisionIds = new Set<string>();
      const cases = showResolvedReviewCases
        ? filteredCases
        : filteredCases.filter((reviewCase) => {
          const decisionId = reviewCase.match_decision.match_decision_id;
          if (seenDecisionIds.has(decisionId)) return false;
          seenDecisionIds.add(decisionId);
          return true;
        });
      setRecommendedReviewCases(cases);
      setReviewCaseIdsByDecision(Object.fromEntries(cases.map((reviewCase) => [
        reviewCase.match_decision.match_decision_id,
        reviewCase.review_case_id,
      ])));
      onTotalLoaded(cases.length);
    }).catch((error: unknown) => {
      if (!ignore) setRecommendedReviewError(error instanceof BffError ? error.message : "Failed to load recommended matches.");
    }).finally(() => {
      if (!ignore) setRecommendedReviewLoading(false);
    });
    return () => { ignore = true; };
  }, [personId, onTotalLoaded, showResolvedReviewCases]);

  useEffect(() => {
    const cleanup = reloadRecommendedReviewCases();
    return cleanup;
  }, [reloadRecommendedReviewCases]);

  const recommendedMatches = (recommendedResult.rows ?? []).filter(
    (match) => (match.decision === "merge" || match.decision === "review")
      && match.left_person_id !== null
      && match.right_person_id !== null,
  );
  useEffect(() => {
    let ignore = false;
    void bffFetchEnvelope<ReviewCaseSummary[]>(
      `/bff/review-cases?person_id=${encodeURIComponent(personId)}&resolved=false&limit=100`,
    ).then((response) => {
      if (ignore) return;
      setOpenReviewDecisionIds(new Set(response.data.map((reviewCase) => reviewCase.match_decision.match_decision_id)));
    }).catch(() => {
      if (!ignore) setOpenReviewDecisionIds(new Set());
    });
    return () => { ignore = true; };
  }, [personId]);

  const recommendedPersonIds = recommendedReviewCases
    .map((reviewCase) => {
      const rightId = reviewCase.right_person_id ?? null;
      const leftId = reviewCase.left_person_id ?? null;
      if (rightId !== null && rightId !== personId) return rightId;
      if (leftId !== null && leftId !== personId) return leftId;
      return null;
    })
    .filter((id): id is string => id !== null);
  const mergeHistoryRows = (mergeHistoryResult.rows ?? []).filter((event) => event.event_type === "manual_merge" || event.event_type === "unmerge");
  const recommendedPersonIdsKey = recommendedPersonIds.join("|");

  useEffect(() => {
    const missingIds = recommendedPersonIds.filter((id) => recommendedPeople[id] === undefined);
    if (missingIds.length === 0) return;
    let ignore = false;
    void Promise.all(
      missingIds.map(async (id) => [id, await bffFetch<Person>(`/bff/persons/${encodeURIComponent(id)}`)] as const),
    ).then((items) => {
      if (ignore) return;
      setRecommendedPeople((prev) => {
        const next = { ...prev };
        for (const [id, person] of items) next[id] = person;
        return next;
      });
    }).catch(() => undefined);
    return () => { ignore = true; };
  }, [recommendedPersonIdsKey, recommendedPeople, recommendedPersonIds]);

  useEffect(() => {
    const missingIds = recommendedPersonIds.filter((id) => recommendedIdentifiers[id] === undefined);
    if (missingIds.length === 0) return;
    let ignore = false;
    void Promise.all(
      missingIds.map(async (id) => {
        try {
          const result = await bffFetchEnvelope<PersonIdentifier[]>(`/bff/persons/${encodeURIComponent(id)}/identifiers`);
          return [id, result.data] as const;
        } catch {
          return [id, []] as const;
        }
      }),
    ).then((items) => {
      if (ignore) return;
      setRecommendedIdentifiers((prev) => ({ ...prev, ...Object.fromEntries(items) }));
    });
    return () => { ignore = true; };
  }, [recommendedPersonIdsKey, recommendedIdentifiers, recommendedPersonIds]);

  const loadCandidateDetail = useCallback(async (candidateId: string): Promise<PossibleMatchDetail | undefined> => {
    if (candidateDetails[candidateId]) return candidateDetails[candidateId];
    setLoadingCandidateId(candidateId);
    setCandidateErrors((prev) => ({ ...prev, [candidateId]: "" }));
    try {
      const detail = await bffFetch<PossibleMatchDetail>(`/bff/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidateId)}/detail`);
      setCandidateDetails((prev) => ({ ...prev, [candidateId]: detail }));
      return detail;
    } catch (e) {
      setCandidateErrors((prev) => ({ ...prev, [candidateId]: e instanceof Error ? e.message : "Failed to load match detail." }));
      return undefined;
    } finally {
      setLoadingCandidateId(null);
    }
  }, [candidateDetails, personId]);

  const loadReviewCaseDetail = useCallback(async (reviewCaseId: string): Promise<void> => {
    if (reviewCaseDetails[reviewCaseId] !== undefined) return;
    try {
      const detail = await bffFetch<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}`);
      setReviewCaseDetails((prev) => ({ ...prev, [reviewCaseId]: detail }));
    } catch {
      // silently ignore — fall back to empty reasons/conflicts
    }
  }, [reviewCaseDetails]);

  useEffect(() => {
    for (const reviewCase of recommendedReviewCases) {
      const rightId = reviewCase.right_person_id ?? null;
      const leftId = reviewCase.left_person_id ?? null;
      const candidateId = (rightId !== null && rightId !== personId) ? rightId : leftId;
      if (candidateId === null || recommendedDetails[reviewCase.review_case_id] !== undefined || recommendedErrors[reviewCase.review_case_id] !== undefined) continue;
      setRecommendedErrors((prev) => ({ ...prev, [reviewCase.review_case_id]: "" }));
      void bffFetch<PossibleMatchDetail>(`/bff/persons/${encodeURIComponent(personId)}/shared-identifiers/${encodeURIComponent(candidateId)}/detail`)
        .then((detail) => {
          setRecommendedDetails((prev) => ({ ...prev, [reviewCase.review_case_id]: detail }));
        })
        .catch((e: unknown) => {
          setRecommendedErrors((prev) => ({ ...prev, [reviewCase.review_case_id]: e instanceof Error ? e.message : "Failed to load match comparison." }));
        });
    }
  }, [personId, recommendedDetails, recommendedErrors, recommendedReviewCases]);

  useEffect(() => {
    const missingIds = recommendedReviewCases
      .map((rc) => rc.review_case_id)
      .filter((id) => reviewCaseDetails[id] === undefined);
    if (missingIds.length === 0) return;
    for (const id of missingIds) {
      void bffFetch<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(id)}`)
        .then((detail) => { setReviewCaseDetails((prev) => ({ ...prev, [id]: detail })); })
        .catch(() => undefined);
    }
  }, [recommendedReviewCases, reviewCaseDetails]);

  const toggleCandidate = useCallback((candidateId: string): void => {
    if (expandedCandidate === candidateId) {
      setExpandedCandidate(null);
      return;
    }
    setExpandedCandidate(candidateId);
    void loadCandidateDetail(candidateId);
  }, [expandedCandidate, loadCandidateDetail]);

  function mergeEventForReviewCase(reviewCase: ReviewCaseSummary): PersonAuditEvent | null {
    const isClosed = reviewCase.queue_state === "resolved" || reviewCase.queue_state === "cancelled";
    if (!isClosed) return null;
    // For resolved cases, only look for a merge event when the decision was "merge".
    // Cancelled cases (auto-closed by CLOSE_PERSON_PAIR_CASES_FOR_ABSORBED) always need the lookup.
    if (reviewCase.queue_state === "resolved" && reviewCase.match_decision.decision !== "merge") return null;
    const leftId = reviewCase.left_person_id ?? null;
    const rightId = reviewCase.right_person_id ?? null;
    if (leftId === null || rightId === null) return null;
    return mergeHistoryRows.find((event) =>
      event.event_type === "manual_merge"
      && event.absorbed_person_id !== null
      && event.survivor_person_id !== null
      && ((event.absorbed_person_id === leftId && event.survivor_person_id === rightId)
        || (event.absorbed_person_id === rightId && event.survivor_person_id === leftId)),
    ) ?? null;
  }

  async function recreateReviewCase(reviewCaseId: string): Promise<void> {
    setRecommendedActionError(null);
    try {
      await bffFetchEnvelope<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}/recreate`, {
        method: "POST",
      });
      window.location.reload();
    } catch (error: unknown) {
      setRecommendedActionError(error instanceof BffError ? error.message : "Failed to recreate review case.");
    }
  }

  async function recreateAndUnmergeReviewCase(reviewCaseId: string, mergeEventId: string): Promise<void> {
    setRecommendedActionError(null);
    try {
      const body: UnmergeRequestBody = {
        merge_event_id: mergeEventId,
        reason: "Recreate review case and unmerge profiles.",
      };
      await bffFetchEnvelope<UnmergeResponseBody>("/bff/persons/unmerge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await bffFetchEnvelope<ReviewCaseDetail>(`/bff/review-cases/${encodeURIComponent(reviewCaseId)}/recreate`, {
        method: "POST",
      });
      window.location.reload();
    } catch (error: unknown) {
      setRecommendedActionError(error instanceof BffError ? error.message : "Failed to recreate and unmerge review case.");
    }
  }

  async function submitUnmerge(): Promise<void> {
    if (unmergeTarget === null || !unmergeReason.trim()) return;
    setUnmergeSubmitting(true);
    setUnmergeError(null);
    try {
      const body: UnmergeRequestBody = { merge_event_id: unmergeTarget.merge_event_id, reason: unmergeReason.trim() };
      await bffFetchEnvelope<UnmergeResponseBody>("/bff/persons/unmerge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      window.location.reload();
    } catch (e) {
      setUnmergeError(e instanceof BffError ? e.message : "Failed to unmerge profiles.");
    } finally {
      setUnmergeSubmitting(false);
    }
  }

  const unmergedMergeEventIds = useMemo(() =>
    new Set(mergeHistoryRows.filter((event) => event.event_type === "unmerge").map((event) => event.metadata?.original_merge_event_id).filter((id): id is string => Boolean(id))),
  [mergeHistoryRows]);
  const hasRecommendations = recommendedReviewCases.length > 0;
  const recommendationCount = recommendedReviewCases.length;
  const actionableResolvedReviewCaseIds = useMemo(() => {
    if (!showResolvedReviewCases) return new Set<string>();
    const seenDecisionIds = new Set<string>();
    const ids = new Set<string>();
    for (const reviewCase of recommendedReviewCases) {
      const decisionId = reviewCase.match_decision.match_decision_id;
      if (seenDecisionIds.has(decisionId)) continue;
      seenDecisionIds.add(decisionId);
      ids.add(reviewCase.review_case_id);
    }
    return ids;
  }, [recommendedReviewCases, showResolvedReviewCases]);

  let tabContent: ReactElement;
  if (activeMatchesTab === "candidates" || activeMatchesTab === "resolved-cases") {
    if (recommendedReviewLoading) {
      tabContent = <TabSkelShell title="Recommended matches"><SkeletonMatches /></TabSkelShell>;
    } else if (recommendedReviewError !== null) {
      tabContent = <section className={styles.contentCard}><div className={styles.tabError}>{recommendedReviewError}</div></section>;
    } else if (!hasRecommendations) {
      tabContent = <MatchEmptyState message="No recommended matches found." />;
    } else {
      tabContent = (
        <div className={styles.matchStack}>
          {hasRecommendations && (
            <div className={styles.matchSubsection}>
              <div className={styles.matchSubheader}>
                {showResolvedReviewCases ? "Resolved review cases" : "Recommended matches"}
                <span className={styles.connSectionCount}>{recommendationCount}</span>
              </div>
              {recommendedActionError !== null ? <div className={styles.tabError}>{recommendedActionError}</div> : null}
              <div className={styles.matchList}>
                {recommendedReviewCases.map((reviewCase) => {
                  const rightId = reviewCase.right_person_id ?? null;
                  const leftId = reviewCase.left_person_id ?? null;
                  const candidateId = (rightId !== null && rightId !== personId) ? rightId : (leftId ?? "");
                  const mergeEvent = mergeEventForReviewCase(reviewCase);
                  const hasOpenCase = openReviewDecisionIds.has(reviewCase.match_decision.match_decision_id);
                  const isActionableResolvedCase = !showResolvedReviewCases || actionableResolvedReviewCaseIds.has(reviewCase.review_case_id);
                  const nonSurvivorIsInactive = (reviewCase.left_person_status !== undefined && reviewCase.left_person_status !== null && reviewCase.left_person_status !== "active")
                    || (reviewCase.right_person_status !== undefined && reviewCase.right_person_status !== null && reviewCase.right_person_status !== "active");
                  return (
                    <RecommendedReviewCaseRow
                      key={reviewCase.review_case_id}
                      reviewCase={reviewCase}
                      currentPerson={currentPerson}
                      candidatePerson={recommendedPeople[candidateId]}
                      currentIdentifiers={currentIdentifiers}
                      candidateIdentifiers={recommendedIdentifiers[candidateId]}
                      detail={recommendedDetails[reviewCase.review_case_id]}
                      reviewCaseDetail={reviewCaseDetails[reviewCase.review_case_id]}
                      error={recommendedErrors[reviewCase.review_case_id] || undefined}
                      isLoading={recommendedDetails[reviewCase.review_case_id] === undefined && recommendedErrors[reviewCase.review_case_id] === ""}
                      isOpen={expandedRecommendedMatch === reviewCase.review_case_id}
                      onToggle={() => {
                        const isOpening = expandedRecommendedMatch !== reviewCase.review_case_id;
                        setExpandedRecommendedMatch((current) => current === reviewCase.review_case_id ? null : reviewCase.review_case_id);
                        if (isOpening) void loadReviewCaseDetail(reviewCase.review_case_id);
                      }}
                      onReview={() => setReviewActionCase(reviewCase)}
                      onView={() => setViewingReviewCaseId(reviewCase.review_case_id)}
                      onRecreate={hasOpenCase || nonSurvivorIsInactive || !isActionableResolvedCase ? undefined : () => void recreateReviewCase(reviewCase.review_case_id)}
                      onRecreateAndUnmerge={isActionableResolvedCase && ((reviewCase.queue_state !== "resolved" && nonSurvivorIsInactive && mergeEvent !== null) || (!hasOpenCase && nonSurvivorIsInactive && mergeEvent !== null)) ? () => void recreateAndUnmergeReviewCase(reviewCase.review_case_id, mergeEvent.merge_event_id) : null}
                      needsUnmergeBeforeReview={reviewCase.queue_state !== "resolved" && nonSurvivorIsInactive}
                    />
                  );
                })}
              </div>
            </div>
          )}
        </div>
      );
    }
  } else if (mergeHistoryResult.loading) {
    tabContent = <TabSkelShell title="Merge History"><SkeletonMatches /></TabSkelShell>;
  } else if (mergeHistoryResult.error) {
    tabContent = <section className={styles.contentCard}><div className={styles.tabError}>{mergeHistoryResult.error}</div></section>;
  } else if (mergeHistoryRows.length === 0) {
    tabContent = <MatchEmptyState message="No merge history yet." />;
  } else {
    tabContent = (
      <div className={styles.auditList}>
        {mergeHistoryRows.map((event, i) => (
          <div key={event.merge_event_id} className={styles.auditItem}>
            <div className={styles.auditRail}>
              <div className={styles.auditDot} />
              {i < mergeHistoryRows.length - 1 && <div className={styles.auditLine} />}
            </div>
            <div className={styles.auditBody}>
              <div className={styles.auditTop}>
                <span className={styles.auditEventType}>{event.event_type}</span>
                <span className={styles.auditTime}>{fmtDateTime(event.created_at)}</span>
              </div>
              <div className={styles.auditActor}>{event.actor_type}:{event.actor_id}</div>
              {event.reason && <div className={styles.auditReason}>{event.reason}</div>}
              {event.event_type === "manual_merge" && !unmergedMergeEventIds.has(event.merge_event_id) && (
                <button type="button" className={styles.auditUnmergeBtn} onClick={() => { setUnmergeTarget(event); setUnmergeReason(""); setUnmergeError(null); }}>Unmerge</button>
              )}
            </div>
          </div>
        ))}
        {(mergeHistoryResult.hasPrev || mergeHistoryResult.hasNext) && (
          <TabPagination from={mergeHistoryResult.from} to={mergeHistoryResult.to} total={mergeHistoryResult.total} hasPrev={mergeHistoryResult.hasPrev} hasNext={mergeHistoryResult.hasNext} onPrev={mergeHistoryResult.goPrev} onNext={mergeHistoryResult.goNext} />
        )}
      </div>
    );
  }

  const reviewActionCaseId = reviewActionCase?.review_case_id ?? null;

  return (
    <>
      {tabContent}
      {reviewActionCase !== null ? (
        <div className={styles.shareOverlay} onClick={() => setReviewActionCase(null)}>
          <div className={`${styles.overrideModal} ${styles.recommendedReviewModal}`} onClick={(event) => event.stopPropagation()}>
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Review recommended match</span>
              <button type="button" className={styles.shareModalClose} onClick={() => setReviewActionCase(null)} aria-label="Close">×</button>
            </div>
            {reviewActionCaseId !== null ? (
              <ReviewActionsPanel
                reviewCaseId={reviewActionCaseId}
                queueState={reviewActionCase.queue_state}
                assignedTo={reviewActionCase.assigned_to}
                leftPersonId={reviewActionCase.left_person_id ?? null}
                rightPersonId={reviewActionCase.right_person_id ?? null}
                leftPersonStatus={reviewActionCase.left_person_status ?? null}
                rightPersonStatus={reviewActionCase.right_person_status ?? null}
                onChanged={() => window.location.reload()}
                embedded
              />
            ) : null}
          </div>
        </div>
      ) : null}
      {unmergeTarget !== null && (
        <div className={styles.shareOverlay} onClick={() => setUnmergeTarget(null)}>
          <div className={`${styles.overrideModal} ${styles.mergeModal} ${styles.unmergeModal}`} onClick={(e) => e.stopPropagation()}>
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Unmerge profiles</span>
              <button type="button" className={styles.shareModalClose} onClick={() => setUnmergeTarget(null)} disabled={unmergeSubmitting} aria-label="Close">×</button>
            </div>
            <p className={styles.shareModalDesc}>This will reverse the merge event and restore the absorbed profile. Please provide a reason for the audit trail.</p>
            <label className={styles.mergeReasonLabel}>
              Reason
              <textarea className={styles.mergeReasonInput} value={unmergeReason} onChange={(e) => setUnmergeReason(e.target.value)} placeholder="Why are you unmerging these profiles?" autoFocus />
            </label>
            {unmergeError !== null && <div className={styles.mergeErrorBox}><span className={styles.mergeErrorIcon} aria-hidden="true">!</span><span>{unmergeError}</span></div>}
            <div className={styles.mergeModalActions}>
              <button type="button" className={styles.secondaryBtn} onClick={() => setUnmergeTarget(null)} disabled={unmergeSubmitting}>Cancel</button>
              <button type="button" className={styles.dangerBtn} onClick={() => void submitUnmerge()} disabled={!unmergeReason.trim() || unmergeSubmitting}>Unmerge profiles</button>
            </div>
          </div>
        </div>
      )}
      <ReviewCaseDetailModal
        open={viewingReviewCaseId !== null}
        reviewCaseId={viewingReviewCaseId ?? ""}
        onClose={() => {
          setViewingReviewCaseId(null);
          reloadRecommendedReviewCases();
        }}
      />
    </>
  );
}

function DecisionHistoryTab({ personId, onTotalLoaded }: { personId: string; onTotalLoaded: (n: number) => void }): ReactElement {
  const [expandedMatch, setExpandedMatch] = useState<string | null>(null);
  const decisionsResult = usePaginatedFetch<PersonMatchDecision>(
    `/bff/persons/${encodeURIComponent(personId)}/matches`,
  );
  const decisions = decisionsResult.rows ?? [];

  useEffect(() => {
    if (!decisionsResult.loading) {
      onTotalLoaded(decisionsResult.total ?? decisions.length);
    }
  }, [decisionsResult.loading, decisionsResult.total, decisions.length, onTotalLoaded]);

  if (decisionsResult.loading) return <TabSkelShell title="Decision History"><SkeletonMatches /></TabSkelShell>;
  if (decisionsResult.error) return <section className={styles.contentCard}><div className={styles.tabError}>{decisionsResult.error}</div></section>;

  return (
    <div className={styles.decisionHistorySection}>
      {decisions.length === 0 ? (
        <MatchEmptyState message="No decision history recorded." />
      ) : (
        <>
          <div className={styles.matchList}>
            {decisions.map((match, index) => {
              const rowKey = `${match.match_decision_id}-${index}`;
              const isExpanded = expandedMatch === rowKey;
              return (
                <div key={rowKey} className={`${styles.matchRow} ${isExpanded ? styles.matchRowOpen : ""}`}>
                  <button type="button" className={styles.matchRowButton} onClick={() => setExpandedMatch(isExpanded ? null : rowKey)} aria-expanded={isExpanded}>
                    <span className={styles.matchDecisionCell}>{match.decision}</span>
                    <span className={styles.matchEngineCell}>{titleCase(match.engine_type)}</span>
                    <span className={styles.matchPersonCell}>{match.left_person_id ?? "—"} ↔ {match.right_person_id ?? "—"}</span>
                    <span className={styles.matchConfidenceCell}>{Math.round(match.confidence * 100)}%</span>
                    <span className={styles.matchCreatedCell}>{fmtDate(match.created_at)}</span>
                    <span className={`${styles.matchChevron} ${isExpanded ? styles.matchChevronOpen : ""}`}>⌄</span>
                  </button>
                  {isExpanded && (
                    <div className={styles.matchDetail}>
                      <div className={styles.matchMetaGrid}>
                        <div className={styles.matchMetaItem}>
                          <span className={styles.matchMetaLabel}>Engine version</span>
                          <span className={styles.matchMetaValue}>{match.engine_version || "—"}</span>
                        </div>
                        <div className={styles.matchMetaItem}>
                          <span className={styles.matchMetaLabel}>Policy</span>
                          <span className={styles.matchMetaValue}>{match.policy_version || "—"}</span>
                        </div>
                        <div className={styles.matchReasonsBlock}>
                          <span className={styles.matchMetaLabel}>Reasons</span>
                          <div className={styles.matchReasonList}>
                            {match.reasons.length > 0 ? match.reasons.map((reason) => (
                              <span key={reason} className={styles.matchReasonChip}>{reason}</span>
                            )) : <span className={styles.matchEmptyText}>No reasons recorded.</span>}
                          </div>
                        </div>
                        <div className={styles.matchReasonsBlock}>
                          <span className={styles.matchMetaLabel}>Blocking conflicts</span>
                          <div className={styles.matchReasonList}>
                            {match.blocking_conflicts.length > 0 ? match.blocking_conflicts.map((conflict) => (
                              <span key={conflict} className={styles.matchReasonChip}>{conflict}</span>
                            )) : <span className={styles.matchEmptyText}>No conflicts recorded.</span>}
                          </div>
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
  const [unmergeTarget, setUnmergeTarget] = useState<PersonAuditEvent | null>(null);
  const [unmergeReason, setUnmergeReason] = useState("");
  const [unmergeSubmitting, setUnmergeSubmitting] = useState(false);
  const [unmergeError, setUnmergeError] = useState<string | null>(null);

  async function submitUnmerge(): Promise<void> {
    if (unmergeTarget === null || !unmergeReason.trim()) return;
    setUnmergeSubmitting(true);
    setUnmergeError(null);
    try {
      const body: UnmergeRequestBody = { merge_event_id: unmergeTarget.merge_event_id, reason: unmergeReason.trim() };
      await bffFetchEnvelope<UnmergeResponseBody>("/bff/persons/unmerge", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      window.location.reload();
    } catch (e) {
      setUnmergeError(e instanceof BffError ? e.message : "Failed to unmerge profiles.");
    } finally {
      setUnmergeSubmitting(false);
    }
  }

  useEffect(() => { if (total !== null) onTotalLoaded(total); }, [total, onTotalLoaded]);

  const unmergedMergeEventIds = useMemo(() => new Set(audit
    .filter((event) => event.event_type === "unmerge")
    .map((event) => event.metadata.original_merge_event_id)
    .filter((mergeEventId): mergeEventId is string => Boolean(mergeEventId))), [audit]);

  if (loading) return <SkeletonAudit />;
  if (error) return <div className={styles.tabError}>{error}</div>;

  return (
    <>
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
              {event.event_type === "manual_merge" && !unmergedMergeEventIds.has(event.merge_event_id) && <button type="button" className={styles.auditUnmergeBtn} onClick={() => { setUnmergeTarget(event); setUnmergeReason(""); setUnmergeError(null); }}>Unmerge</button>}
            </div>
          </div>
        ))}
      </div>
      {(hasPrev || hasNext) && <TabPagination from={from} to={to} total={total} hasPrev={hasPrev} hasNext={hasNext} onPrev={goPrev} onNext={goNext} />}
      {unmergeTarget !== null && (
        <div className={styles.shareOverlay} onClick={() => setUnmergeTarget(null)}>
          <div className={`${styles.overrideModal} ${styles.mergeModal} ${styles.unmergeModal}`} onClick={(e) => e.stopPropagation()}>
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Unmerge profiles</span>
              <button type="button" className={styles.shareModalClose} onClick={() => setUnmergeTarget(null)} disabled={unmergeSubmitting} aria-label="Close">×</button>
            </div>
            <div className={styles.overrideFieldGroup}>
              <div className={styles.overrideLabel}>Reason</div>
              <textarea className={styles.mergeReasonInput} value={unmergeReason} onChange={(e) => setUnmergeReason(e.target.value)} placeholder="Why are you unmerging these profiles?" autoFocus />
            </div>
            {unmergeError !== null && <div className={styles.mergeError}>{unmergeError}</div>}
            <div className={styles.mergeModalActions}>
              <button type="button" className={styles.secondaryBtn} onClick={() => setUnmergeTarget(null)} disabled={unmergeSubmitting}>Cancel</button>
              <button type="button" className={styles.dangerBtn} onClick={() => void submitUnmerge()} disabled={!unmergeReason.trim() || unmergeSubmitting}>Unmerge profiles</button>
            </div>
          </div>
        </div>
      )}
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

type SourceRecordViewMode = "summary" | "conversation" | "raw";

function SourceRecordRow({ record }: { record: PersonSourceRecord }): ReactElement {
  const [open, setOpen] = useState(false);
  const [rawOpen, setRawOpen] = useState(false);
  const isConversation = record.record_type === "conversation" && (record.chat_transcript?.length ?? 0) > 0;
  const hasSummary = (record.normalized_payload?.summary?.trim().length ?? 0) > 0;
  const [viewMode, setViewMode] = useState<SourceRecordViewMode>(
    isConversation ? "conversation" : "raw",
  );
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
        <div className={styles.srcBody}>
          <span className={styles.srcTitle}>{titleCase(record.source_system)}</span>
          <div className={styles.srcMetaLine}>
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
        <div className={styles.srcBadges}>
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

          {isConversation && (
            <div className={styles.srViewSwitch} role="tablist" aria-label="Conversation view mode">
              <button
                type="button"
                role="tab"
                aria-selected={viewMode === "summary"}
                className={`${styles.srViewSwitchBtn} ${viewMode === "summary" ? styles.srViewSwitchBtnOn : ""}`}
                onClick={() => setViewMode("summary")}
                disabled={!hasSummary}
                title={hasSummary ? "Show summary" : "No summary available"}
              >
                Summary
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={viewMode === "conversation"}
                className={`${styles.srViewSwitchBtn} ${viewMode === "conversation" ? styles.srViewSwitchBtnOn : ""}`}
                onClick={() => setViewMode("conversation")}
              >
                Conversation
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={viewMode === "raw"}
                className={`${styles.srViewSwitchBtn} ${viewMode === "raw" ? styles.srViewSwitchBtnOn : ""}`}
                onClick={() => setViewMode("raw")}
              >
                Raw data
              </button>
            </div>
          )}

          {isConversation && hasSummary && viewMode === "summary" && (
            <div className={styles.idDetailSection}>
              <div className={styles.idDetailSectionTitle}>Summary</div>
              <p className={styles.srSummary}>{record.normalized_payload?.summary}</p>
            </div>
          )}

          {isConversation && viewMode === "summary" && !hasSummary && (
            <div className={styles.idDetailSection}>
              <div className={styles.idDetailSectionTitle}>Summary</div>
              <div className={styles.srMetaValue}>No summary available for this record.</div>
            </div>
          )}

          {record.chat_transcript !== null && record.chat_transcript.length > 0 && isConversation && viewMode === "conversation" && (
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

          {record.raw_payload !== null && (isConversation ? viewMode === "raw" : true) && (
            <div className={styles.idDetailSection}>
              <button type="button" className={styles.srRawToggle} onClick={() => setRawOpen((v) => !v)} aria-expanded={rawOpen || (isConversation && viewMode === "raw")}>
                <svg className={`${styles.srcChevron} ${(rawOpen || (isConversation && viewMode === "raw")) ? styles.srcChevronOpen : ""}`} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6" /></svg>
                Raw payload <span className={styles.srMetaLabel}>(original source JSON)</span>
              </button>
              {(rawOpen || (isConversation && viewMode === "raw")) && <pre className={styles.srJson}>{JSON.stringify(record.raw_payload, null, 2)}</pre>}
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
  const sourceRecordParams = new URLSearchParams();
  if (activeEntity !== null) sourceRecordParams.set("entity_key", activeEntity);
  const queryString = sourceRecordParams.toString();
  const basePath = `/bff/persons/${encodeURIComponent(personId)}/source-records${queryString ? `?${queryString}` : ""}`;

  useEffect(() => { onTotalLoaded(facetTotal); }, [facetTotal, onTotalLoaded]);

  return (
    <section className={styles.contentCard}>
      <div className={styles.connHeader}>
        <span className={styles.connHeaderTitle}>Source records</span>
        <span className={styles.connHeaderDot}>·</span>
        <span className={styles.connHeaderCount}>{facetTotal} {facetTotal === 1 ? "record" : "records"}</span>
      </div>

      {facets.length > 0 && (
        <div className={styles.srFilterGroup}>
          <span className={styles.srFilterLabel}>Source</span>
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
  const sectionLabels = ["Matches", "Source records", "Sales", "Connections", "Identifiers", "Decision History"];
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
            {/* avatar + name + badges */}
            <div className={styles.profileHero}>
              <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                <span className={styles.skelRing} style={{ width: 64, height: 64, flexShrink: 0 }} />
                <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                  <span className={styles.skel} style={{ width: 140, height: 16 }} />
                  <div style={{ display: "flex", gap: 6 }}>
                    <span className={styles.skel} style={{ width: 52, height: 20, borderRadius: 6 }} />
                    <span className={styles.skel} style={{ width: 68, height: 20, borderRadius: 6 }} />
                    <span className={styles.skel} style={{ width: 80, height: 20, borderRadius: 6 }} />
                  </div>
                </div>
              </div>
            </div>

            {/* GP fields */}
            <div className={styles.sidebarHeroSummary}>
              {["Phone", "Email", "DOB", "NRIC"].map((_, i) => (
                <div key={i} className={styles.sidebarHeroSummaryRow}>
                  <span className={styles.skel} style={{ width: 48, height: 10 }} />
                  <span className={styles.skel} style={{ width: [120, 150, 90, 110][i], height: 12 }} />
                </div>
              ))}
            </div>

            {/* Detail toggle */}
            <div style={{ borderTop: "1px solid var(--border)", paddingTop: 10 }}>
              <span className={styles.skel} style={{ width: 50, height: 12 }} />
            </div>
          </section>

          {/* Bankruptcy sidebar card */}
          <section className={styles.sidebarCard}>
            <span className={styles.skel} style={{ width: 100, height: 12, marginBottom: 8 }} />
            <span className={styles.skel} style={{ width: 160, height: 11 }} />
          </section>

          {/* Graph card */}
          <section className={styles.sidebarCard}>
            <div className={styles.sourceEntityHeader}>
              <span className={styles.skel} style={{ width: 80, height: 12 }} />
            </div>
            <div style={{ height: 160, display: "flex", alignItems: "center", justifyContent: "center" }}>
              <span className={styles.skelCircle} style={{ width: 100, height: 100 }} />
            </div>
          </section>

          {/* Timeline card */}
          <section className={styles.sidebarCard}>
            <div className={styles.sourceEntityHeader}>
              <span className={styles.skel} style={{ width: 60, height: 12 }} />
            </div>
            {SKEL_N.slice(0, 3).map((i) => (
              <div key={i} style={{ display: "flex", gap: 8, alignItems: "flex-start", padding: "6px 0" }}>
                <span className={styles.skel} style={{ width: 8, height: 8, borderRadius: 4, flexShrink: 0, marginTop: 3 }} />
                <div style={{ display: "flex", flexDirection: "column", gap: 4, flex: 1 }}>
                  <span className={styles.skel} style={{ width: `${70 + (i % 3) * 15}%`, height: 11 }} />
                  <span className={styles.skel} style={{ width: `${50 + (i % 2) * 20}%`, height: 9 }} />
                </div>
              </div>
            ))}
          </section>
        </aside>

        {/* ── Main column ── */}
        <div className={styles.mainColumn}>
          {/* Section nav tabs */}
          <div className={styles.rightTabsInline}>
            <div className={styles.tabs}>
              {sectionLabels.map((label) => (
                <span key={label} className={styles.tab} style={{ cursor: "default" }}>
                  <span className={styles.skel} style={{ width: label.length * 7, height: 13 }} />
                </span>
              ))}
            </div>
          </div>

          {/* Bento sections grid */}
          <div className={styles.tabPanelScroll}>
            <div className={styles.collapsibleSectionsContainer}>
              {/* Matches — full width */}
              <section className={styles.collapsibleSection}>
                <div className={styles.collapsibleHeader}>
                  <span className={styles.skel} style={{ width: 80, height: 14 }} />
                </div>
                <SkeletonMatches />
              </section>

              {/* Source records — full width */}
              <section className={styles.collapsibleSection}>
                <div className={styles.collapsibleHeader}>
                  <span className={styles.skel} style={{ width: 100, height: 14 }} />
                </div>
                <SkeletonRows />
              </section>

              {/* Sales — top half */}
              <section className={`${styles.collapsibleSection} ${styles.collapsibleSectionTop}`}>
                <div className={styles.collapsibleHeader}>
                  <span className={styles.skel} style={{ width: 100, height: 14 }} />
                </div>
                <SkeletonRows />
              </section>

              {/* Connections — top half */}
              <section className={`${styles.collapsibleSection} ${styles.collapsibleSectionTop}`}>
                <div className={styles.collapsibleHeader}>
                  <span className={styles.skel} style={{ width: 90, height: 14 }} />
                </div>
                <SkeletonConnections />
              </section>

              {/* Identifiers — full width */}
              <section className={styles.collapsibleSection}>
                <div className={styles.collapsibleHeader}>
                  <span className={styles.skel} style={{ width: 80, height: 14 }} />
                </div>
                <SkeletonRows />
              </section>

              {/* Decision History — full width */}
              <section className={styles.collapsibleSection}>
                <div className={styles.collapsibleHeader}>
                  <span className={styles.skel} style={{ width: 120, height: 14 }} />
                </div>
                <SkeletonAudit />
              </section>
            </div>
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

// Swallows 404 (optional data not present) but re-throws everything else
// so 401/500 errors surface rather than silently showing zero counts.
function catchNotFound(err: unknown): null {
  if (err instanceof BffError && err.status === 404) return null;
  throw err;
}

export default function PersonDetailPage({ params }: { params: Promise<{ personId: string }> }): ReactElement {
  const { personId } = use(params);

  const [person, setPerson] = useState<Person | null>(null);
  const [detailData, setDetailData] = useState<DetailData>(EMPTY_DETAIL);
  const [tabTotals, setTabTotals] = useState<Partial<Record<Tab, number>>>({});
  const [loading, setLoading] = useState(true);
  const pageLoadId = useId();
  const setGlobalLoading = useSetLoading();
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFoundFlag, setNotFoundFlag] = useState(false);
  const sectionScrollRef = useRef<HTMLDivElement | null>(null);
  const [highlightedSectionId, setHighlightedSectionId] = useState<string | null>(null);
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [graphOpen, setGraphOpen] = useState(false);
  const [activeMatchesTab, setActiveMatchesTab] = useState<"candidates" | "resolved-cases" | "merge-history">("candidates");
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
    if (highlightTimerRef.current !== null) clearTimeout(highlightTimerRef.current);
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    setNotFoundFlag(false);
    setGlobalLoading(pageLoadId, true);

    async function loadPersonDetail(): Promise<void> {
      const encodedPersonId = encodeURIComponent(personId);
      try {
        const [personRes, identifiersRes] = await Promise.all([
          bffFetch<Person>(`/bff/persons/${encodedPersonId}`),
          bffFetchEnvelope<PersonIdentifier[]>(`/bff/persons/${encodedPersonId}/identifiers?limit=200`),
        ]);

        if (cancelled) return;
        setPerson(personRes);
        setDetailData((current) => ({ ...current, identifiers: identifiersRes.data }));
        setLoading(false);
      } catch (err) {
        if (cancelled) return;
        setGlobalLoading(pageLoadId, false);
        if (err instanceof BffError && err.status === 404) {
          setNotFoundFlag(true);
          return;
        }
        setLoadError(err instanceof Error ? err.message : "Failed to load person detail.");
        setLoading(false);
        return;
      }

      try {
        const [sourceRecordsRes, salesRes, auditRes, bankruptcyRes, sourceRecordFacetsRes] = await Promise.all([
          bffFetchEnvelope<PersonSourceRecord[]>(`/bff/persons/${encodedPersonId}/source-records?limit=20`),
          bffFetchEnvelope<SalesOrder[]>(`/bff/persons/${encodedPersonId}/sales?limit=20`).catch(catchNotFound),
          bffFetchEnvelope<PersonAuditEvent[]>(`/bff/persons/${encodedPersonId}/audit?limit=20`).catch(catchNotFound),
          bffFetchEnvelope<PersonBankruptcyCase[]>(`/bff/persons/${encodedPersonId}/bankruptcy-cases?limit=20`).catch(catchNotFound),
          bffFetchEnvelope<SourceRecordEntityFacet[]>(`/bff/persons/${encodedPersonId}/source-record-entities`).catch(catchNotFound),
        ]);

        if (cancelled) return;
        setDetailData((current) => ({
          ...current,
          sourceRecords: sourceRecordsRes.data,
          sales: salesRes?.data ?? [],
          audit: auditRes?.data ?? [],
          bankruptcyCases: bankruptcyRes?.data ?? [],
          sourceRecordFacets: sourceRecordFacetsRes?.data ?? [],
        }));
      } catch (err) {
        if (cancelled) return;
        setLoadError(err instanceof Error ? err.message : "Failed to load person detail.");
      } finally {
        if (!cancelled) {
          setGlobalLoading(pageLoadId, false);
        }
      }
    }

    void loadPersonDetail();

    return () => {
      cancelled = true;
      setGlobalLoading(pageLoadId, false);
    };
  }, [pageLoadId, personId, setGlobalLoading]);

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
      { key: "preferred_address", label: "Address", thisRaw: person.preferred_address?.address_id ?? null, thisDisplay: person.preferred_address?.normalized_full ?? null, candidateRaw: target.preferred_address?.address_id ?? null, candidateDisplay: target.preferred_address?.normalized_full ?? null },
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
      if (field.key === "preferred_address") {
        return [{ field_name: field.key, source_kind: "address", selected_value: field.thisRaw, source_record_pk: null, identifier_type: null }];
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
        setMergeSuccessMessage(`Merged successfully. Click OK to open ${mergeTarget.preferred_full_name ?? "the survivor profile"}.`);
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

  function openMergeWithCandidate(candidate: PersonSharedIdentifierCandidate, detail: PossibleMatchDetail | undefined): void {
    const firstGroup = detail?.shared_identifier_groups[0];
    const currentRecord = firstGroup?.current_person_source_records[0];
    const candidateRecord = firstGroup?.candidate_source_records[0];
    const evidenceLabel = firstGroup
      ? `${titleCase(firstGroup.identifier_type)}: ${firstGroup.normalized_value}`
      : candidate.identifiers.map((id) => `${titleCase(id.identifier_type)}: ${id.normalized_value}`).join(" · ");
    chooseMergeTarget({
      person_id: candidate.person_id,
      preferred_full_name: candidate.preferred_full_name,
      preferred_phone: candidate.preferred_phone,
      preferred_email: candidate.preferred_email,
      preferred_dob: candidate.preferred_dob,
      preferred_address: null,
      preferred_nric: null,
      matchSourceLabel: "Possible Duplicates",
      matchEvidence: evidenceLabel,
      currentEvidence: currentRecord ? `${sourceRecordMeta(currentRecord)} · observed ${fmtDate(currentRecord.observed_at)}` : undefined,
      candidateEvidence: candidateRecord ? `${sourceRecordMeta(candidateRecord)} · observed ${fmtDate(candidateRecord.observed_at)}` : undefined,
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
  const [overrideSelections, setOverrideSelections] = useState<Partial<Record<GoldenFieldName, string>>>({});
  const [overrideCustomValues, setOverrideCustomValues] = useState<Partial<Record<GoldenFieldName, string>>>({});
  const [overrideReason, setOverrideReason] = useState("");
  const [overrideSubmitting, setOverrideSubmitting] = useState(false);
  const [overrideError, setOverrideError] = useState<string | null>(null);
  const [overrideSuccess, setOverrideSuccess] = useState(false);
  const [fieldOptions, setFieldOptions] = useState<PersonFieldOptions | null>(null);
  const fieldOptionsRef = useRef<PersonFieldOptions | null>(null);
  const [fieldOptionsLoading, setFieldOptionsLoading] = useState(false);

  useEffect(() => {
    fieldOptionsRef.current = fieldOptions;
  }, [fieldOptions]);

  // Show cached editable fields immediately, then refresh them in the background.
  useEffect(() => {
    if (!overrideOpen) return;
    let cancelled = false;
    const cachedOptions = fieldOptionsRef.current?.person_id === personId ? fieldOptionsRef.current : null;
    const applyOptions = (options: PersonFieldOptions): void => {
      fieldOptionsRef.current = options;
      setFieldOptions(options);
      setOverrideSelections(Object.fromEntries(options.fields.map((field) => [field.field_name, field.options.find((option) => option.is_current)?.source_record_pk ?? field.options[0]?.source_record_pk ?? ""])) as Partial<Record<GoldenFieldName, string>>);
      setOverrideCustomValues({});
    };
    async function loadFieldOptions(): Promise<void> {
      setFieldOptionsLoading(cachedOptions === null);
      setOverrideError(null);
      if (cachedOptions !== null) applyOptions(cachedOptions);
      try {
        const env = await bffFetchEnvelope<PersonFieldOptions>(`/bff/persons/${encodeURIComponent(personId)}/field-options`);
        if (!cancelled) applyOptions(env.data);
      } catch (e) {
        if (!cancelled) setOverrideError(e instanceof BffError ? e.message : "Failed to load editable fields.");
      } finally {
        if (!cancelled) setFieldOptionsLoading(false);
      }
    }
    void loadFieldOptions();
    return () => { cancelled = true; };
  }, [overrideOpen, personId]);

  const selectedOverrideFields = useMemo(() => {
    if (fieldOptions === null) return [];
    return fieldOptions.fields.reduce<OverrideFieldSelection[]>((items, field) => {
      const selectedPk = overrideSelections[field.field_name];
      const currentPk = field.options.find((option) => option.is_current)?.source_record_pk ?? "";
      if (selectedPk === "__custom__") {
        const customValue = overrideCustomValues[field.field_name]?.trim() ?? "";
        if (customValue) items.push({ field, customValue });
        return items;
      }
      if (selectedPk && selectedPk !== currentPk) items.push({ field, sourceRecordPk: selectedPk });
      return items;
    }, []);
  }, [fieldOptions, overrideCustomValues, overrideSelections]);

  async function handleOverrideSubmit(): Promise<void> {
    if (selectedOverrideFields.length === 0 || !overrideReason.trim()) return;
    setOverrideSubmitting(true);
    setOverrideError(null);
    const invalidCustom = selectedOverrideFields.find((item) => "customValue" in item && validateCustomOverrideValue(item.field.field_name, item.customValue) !== null);
    if (invalidCustom !== undefined && "customValue" in invalidCustom) {
      setOverrideSubmitting(false);
      setOverrideError(validateCustomOverrideValue(invalidCustom.field.field_name, invalidCustom.customValue));
      return;
    }
    try {
      await Promise.all(selectedOverrideFields.map((item) => {
        const body: SurvivorshipOverrideRequestBody = {
          field_name: item.field.field_name,
          reason: overrideReason.trim(),
          ...("customValue" in item ? { custom_value: item.customValue } : { source_record_pk: item.sourceRecordPk }),
        };
        return bffFetchEnvelope(`/bff/persons/${encodeURIComponent(personId)}/survivorship-overrides`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      }));
      const [refreshed, refreshedOptions] = await Promise.all([
        bffFetch<Person>(`/bff/persons/${encodeURIComponent(personId)}`).catch(() => null),
        bffFetchEnvelope<PersonFieldOptions>(`/bff/persons/${encodeURIComponent(personId)}/field-options`).catch(() => null),
      ]);
      if (refreshed) setPerson(refreshed);
      if (refreshedOptions) {
        fieldOptionsRef.current = refreshedOptions.data;
        setFieldOptions(refreshedOptions.data);
      }
      setOverrideSuccess(true);
      setTimeout(() => {
        setOverrideOpen(false);
        setOverrideSuccess(false);
        setOverrideReason("");
        setOverrideSelections({});
        setOverrideCustomValues({});
      }, 900);
    } catch (e) {
      setOverrideError(e instanceof BffError ? e.message : "Failed to save override.");
    } finally {
      setOverrideSubmitting(false);
    }
  }

  const onMatchesTotal     = useCallback((n: number) => { setTabTotals((p) => ({ ...p, matches:     n })); }, []);
  const onDecisionHistoryTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, "decision-history": n })); }, []);
  const onConnectionsTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, connections: n })); }, []);
  const onSalesTotal       = useCallback((n: number) => { setTabTotals((p) => ({ ...p, sales:       n })); }, []);
  const onSourceRecordsTotal = useCallback((n: number) => { setTabTotals((p) => ({ ...p, "source-records": n })); }, []);
  const handleSectionJump = useCallback((id: string): void => {
    setHighlightedSectionId(id);
    if (highlightTimerRef.current !== null) clearTimeout(highlightTimerRef.current);
    highlightTimerRef.current = setTimeout(() => setHighlightedSectionId(null), 2600);
  }, []);

  const sections: SectionConfig[] = useMemo(() => [
    { id: "section-matches",        label: "Matches",        count: tabTotals.matches },
    { id: "section-source-records", label: "Source records", count: tabTotals["source-records"] ?? (detailData.sourceRecordFacets.reduce((sum, f) => sum + f.count, 0) || undefined) },
    { id: "section-sales",          label: "Sales",          count: tabTotals.sales          ?? detailData.sales.length },
    { id: "section-connections",    label: "Connections",    count: tabTotals.connections    ?? (person?.connection_count ?? 0) },
    { id: "section-identifiers",    label: "Identifiers",    count: detailData.identifiers.length || undefined },
    { id: "section-decision-history", label: "Decision History", count: tabTotals["decision-history"] },
  ], [tabTotals, detailData, person?.connection_count]);

  if (notFoundFlag) notFound();

  if (loading || person === null) {
    return <PersonDetailSkeleton />;
  }

  if (loadError !== null) {
    return <div style={{ padding: "2rem", color: "var(--text-muted)", fontSize: 14 }}>{loadError}</div>;
  }

  return (
    <div className={styles.page}>
      <PersonBreadcrumb personName={person.preferred_full_name} onShare={() => void handleShare()} shareLoading={shareLoading} />
      <div className={styles.tabContent}>
        <DetailShell person={person} detailData={detailData} personId={personId} salesTotal={tabTotals.sales} sections={sections} scrollRef={sectionScrollRef} onSectionJump={handleSectionJump} onOverride={() => setOverrideOpen(true)} onGraphOpen={() => setGraphOpen(true)}>
          <div className={styles.collapsibleSectionsContainer}>
            {sections.map((section) => {
              let content: ReactElement;
              switch (section.id) {
                case "section-matches":
                  content = <MatchesTab personId={personId} currentPerson={person} currentIdentifiers={detailData.identifiers} activeMatchesTab={activeMatchesTab} onTotalLoaded={onMatchesTotal} onMergeWith={openMergeWithCandidate} />;
                  break;
                case "section-sales":
                  content = <SalesTab personId={personId} onTotalLoaded={onSalesTotal} />;
                  break;
                case "section-connections":
                  content = <ConnectionsTab personId={personId} onTotalLoaded={onConnectionsTotal} />;
                  break;
                case "section-identifiers":
                  content = <IdentifiersTab identifiers={detailData.identifiers} />;
                  break;
                case "section-decision-history":
                  content = <DecisionHistoryTab personId={personId} onTotalLoaded={onDecisionHistoryTotal} />;
                  break;
                case "section-source-records":
                  content = <SourceRecordsTab personId={personId} facets={detailData.sourceRecordFacets} onTotalLoaded={onSourceRecordsTotal} />;
                  break;
                default:
                  return null;
              }

              const bentoClass = section.id === "section-matches" || section.id === "section-source-records"
                ? styles.collapsibleSectionTop
                : "";

              const sectionAction = section.id === "section-matches" ? (
                <div className={styles.matchInnerTabs}>
                  <button type="button" className={`${styles.matchInnerTab}${activeMatchesTab === "candidates" ? ` ${styles.matchInnerTabActive}` : ""}`} onClick={() => setActiveMatchesTab("candidates")}>Matches</button>
                  <button type="button" className={`${styles.matchInnerTab}${activeMatchesTab === "resolved-cases" ? ` ${styles.matchInnerTabActive}` : ""}`} onClick={() => setActiveMatchesTab("resolved-cases")}>Resolved</button>
                  <button type="button" className={`${styles.matchInnerTab}${activeMatchesTab === "merge-history" ? ` ${styles.matchInnerTabActive}` : ""}`} onClick={() => setActiveMatchesTab("merge-history")}>History</button>
                </div>
              ) : undefined;

              return (
                <BentoSection key={section.id} section={section} className={bentoClass} highlighted={highlightedSectionId === section.id} action={sectionAction}>
                  {content}
                </BentoSection>
              );
            })}
          </div>
        </DetailShell>
      </div>

      {mergeOpen && (
        <div className={styles.shareOverlay} onClick={closeMerge}>
          <div className={`${styles.overrideModal} ${styles.mergeModal}${mergeSuccess ? ` ${styles.mergeResultModal}` : ""}`} onClick={(e) => e.stopPropagation()}>
            {mergeSubmitting && <MergeLoadingOverlay />}
            <div className={styles.shareModalHeader}>
              <span className={styles.shareModalTitle}>Merge duplicate profiles</span>
              <button type="button" className={styles.shareModalClose} onClick={closeMerge} disabled={mergeSubmitting || mergeSuccess} aria-label="Close">×</button>
            </div>

            {mergeSuccess ? (
              <div className={styles.mergeResultState}>
                <div className={`${styles.mergeResultIcon} ${styles.mergeResultIconSuccess}`} aria-hidden="true">
                  <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M20 6L9 17l-5-5" />
                  </svg>
                </div>
                <div className={styles.mergeResultTitle}>Merge successful</div>
                <div className={styles.mergeResultMessage}>{mergeSuccessMessage ?? "Merged successfully."}</div>
                <button
                  type="button"
                  className={styles.primaryBtn}
                  onClick={() => {
                    if (mergeTarget !== null) window.location.replace(toBasePath(`/persons/${mergeTarget.person_id}`));
                    else window.location.reload();
                  }}
                >
                  OK
                </button>
              </div>
            ) : (
              <>
                {mergeTarget !== null && mergeTarget.currentEvidence && mergeTarget.candidateEvidence && (
                  <div className={styles.mergeEvidenceGrid}>
                    <div className={styles.mergeEvidenceCard}>
                      <span className={styles.mergeSourceLabel}>Current profile evidence</span>
                      <span className={styles.mergeEvidenceValue}>{mergeTarget.currentEvidence}</span>
                    </div>
                    <div className={styles.mergeEvidenceCard}>
                      <span className={styles.mergeSourceLabel}>Candidate evidence</span>
                      <span className={styles.mergeEvidenceValue}>{mergeTarget.candidateEvidence}</span>
                    </div>
                  </div>
                )}

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

                {mergeError && (
                  <div className={styles.mergeErrorBox}>
                    <span className={styles.mergeErrorIcon} aria-hidden="true">!</span>
                    <span>{mergeError}</span>
                  </div>
                )}

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

            <div className={styles.overrideFormRows}>
              {fieldOptionsLoading && <div className={styles.overrideLoadingText}>Loading field values…</div>}
              {!fieldOptionsLoading && (fieldOptions?.fields ?? []).map((field) => {
                const currentOption = field.options.find((option) => option.is_current) ?? field.options[0];
                const candidateOptions = field.options.filter((option) => option.source_record_pk !== currentOption?.source_record_pk);
                const selectedPk = overrideSelections[field.field_name] ?? currentOption?.source_record_pk ?? "";
                const canUseCustomValue = true;
                const selectedCandidatePk = selectedPk === "__custom__" ? "__custom__" : candidateOptions.some((option) => option.source_record_pk === selectedPk) ? selectedPk : currentOption?.source_record_pk ?? "";
                const currentDisplay = field.current_value_display ?? currentOption?.value_display ?? "No value";
                const currentMeta = currentOption
                  ? `${currentOption.entity_display_name ?? currentOption.source_system}${currentOption.observed_at_display ? ` · ${currentOption.observed_at_display}` : ""}`
                  : field.is_overridden
                    ? "Custom override"
                    : "No current value";
                return (
                  <div key={field.field_name} className={styles.overrideFormRow}>
                    <div className={styles.overrideCurrentValue}>
                      <span className={styles.overrideFormLabel}>{field.label}</span>
                      <span className={styles.overrideCurrentText}>{currentDisplay}</span>
                      <span className={styles.overrideCurrentMeta}>{currentMeta}</span>
                    </div>
                    <div className={styles.overrideCandidateValue}>
                      <select
                        className={styles.overrideCandidateSelect}
                        name={`override-field-${field.field_name}`}
                        value={selectedCandidatePk}
                        disabled={candidateOptions.length === 0 && !canUseCustomValue}
                        onChange={(event) => {
                          const nextPk = event.target.value || (currentOption?.source_record_pk ?? "");
                          setOverrideSelections((current) => ({ ...current, [field.field_name]: nextPk }));
                        }}
                      >
                        <option value={currentOption?.source_record_pk ?? ""}>{currentDisplay}</option>
                        {candidateOptions.map((option) => (
                          <option key={`${field.field_name}-${option.source_record_pk}`} value={option.source_record_pk}>
                            {option.value_display} · {option.entity_display_name ?? option.source_system}
                            {option.observed_at_display ? ` · ${option.observed_at_display}` : ""}
                          </option>
                        ))}
                        {canUseCustomValue && <option value="__custom__">Custom value…</option>}
                      </select>
                      {selectedPk === "__custom__" && (
                        <input
                          className={styles.overrideCustomInput}
                          type={customOverrideInputType(field.field_name)}
                          inputMode={customOverrideInputMode(field.field_name)}
                          value={overrideCustomValues[field.field_name] ?? ""}
                          placeholder={customOverridePlaceholder(field.field_name)}
                          onChange={(event) => setOverrideCustomValues((current) => ({ ...current, [field.field_name]: event.target.value }))}
                        />
                      )}
                    </div>
                  </div>
                );
              })}
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
              disabled={selectedOverrideFields.length === 0 || !overrideReason.trim() || overrideSubmitting}
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
