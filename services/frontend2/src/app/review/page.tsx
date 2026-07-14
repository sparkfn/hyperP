"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactElement, type ReactNode } from "react";
import Link from "next/link";

import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ApiResponse } from "@/lib/api-types";
import type { ReviewCaseSummary } from "@/lib/api-types-ops";
import { isOverdue, relativeTime } from "@/lib/display";
import { useSetLoading } from "@/lib/LoadingContext";
import styles from "./review.module.css";

type QueueState = "unresolved" | "open" | "assigned" | "deferred" | "resolved" | "all";
type Priority = "all" | "high" | "medium" | "low";
type SortKey = "priority" | "confidence" | "queue_state" | "sla_due_at";
type SortDir = "asc" | "desc";

// "unresolved"/"resolved" map to the server-side `resolved` boolean (resolved
// includes cancelled cases); the named states map to exact `queue_state`.
const QUEUE_FILTERS: { value: QueueState; label: string }[] = [
  { value: "unresolved", label: "Unresolved" },
  { value: "open", label: "Open" },
  { value: "assigned", label: "Assigned" },
  { value: "deferred", label: "Deferred" },
  { value: "resolved", label: "Resolved" },
  { value: "all", label: "All" },
];

// Priority pills map to a server-side priority range, aligned with priorityLabel().
const PRIORITY_FILTERS: { value: Priority; label: string; gte: number | null; lte: number | null }[] = [
  { value: "all", label: "All priority", gte: null, lte: null },
  { value: "high", label: "High", gte: 80, lte: null },
  { value: "medium", label: "Medium", gte: 50, lte: 79 },
  { value: "low", label: "Low", gte: null, lte: 49 },
];

const DECISION_OPTIONS = ["merge", "review", "no_match"] as const;
const ENGINE_OPTIONS = ["deterministic", "heuristic", "pair_audit", "llm", "manual"] as const;

const PAGE_SIZES = [25, 50, 100];

// Default sort direction per column when first activated.
const DEFAULT_DIR: Record<SortKey, SortDir> = {
  priority: "asc",
  confidence: "desc",
  queue_state: "asc",
  sla_due_at: "asc",
};

function titleCase(value: string): string {
  return value
    .split("_")
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function shortCaseId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 12)}…` : value;
}

function reviewSubjectTitle(c: ReviewCaseSummary): string {
  const left = c.left_person_id ? (c.left_person_name ?? "Unknown name") : null;
  const right = c.right_person_id ? (c.right_person_name ?? "Unknown name") : null;
  if (left !== null && right !== null) return `${left} ↔ ${right}`;
  if (left !== null) return left;
  if (right !== null) return right;
  return `${titleCase(c.match_decision.decision)} case`;
}

function reviewSubjectSubtitle(c: ReviewCaseSummary): string {
  if (c.left_person_id && c.right_person_id) return `Pair audit · Case ref: ${shortCaseId(c.review_case_id)} · ${titleCase(c.match_decision.engine_type)} engine`;
  if (c.left_person_id || c.right_person_id) return `Person review · Case ref: ${shortCaseId(c.review_case_id)} · ${titleCase(c.match_decision.engine_type)} engine`;
  return `Case ref: ${shortCaseId(c.review_case_id)} · ${titleCase(c.match_decision.engine_type)} engine`;
}

function subjectInitials(value: string): string {
  return value
    .split(/\s+|↔/)
    .filter(Boolean)
    .map((part) => part.charAt(0))
    .slice(0, 2)
    .join("")
    .toUpperCase() || "R";
}

function subjectAvatarColor(value: string): string {
  const palette = ["#ef4444", "#f97316", "#8b5cf6", "#06b6d4", "#10b981", "#6366f1", "#ec4899"];
  let hash = 0;
  for (let i = 0; i < value.length; i += 1) hash = (hash * 31 + value.charCodeAt(i)) >>> 0;
  return palette[hash % palette.length] ?? "#6366f1";
}

function queueStateClass(s: string): string {
  if (s === "open") return styles.badgeOpen ?? "";
  if (s === "assigned") return styles.badgeAssigned ?? "";
  if (s === "deferred") return styles.badgeDeferred ?? "";
  if (s === "resolved") return styles.badgeResolved ?? "";
  return styles.badgeNeutral ?? "";
}

function priorityLabel(p: number): string {
  if (p >= 80) return "High";
  if (p >= 50) return "Medium";
  return "Low";
}
function priorityClass(p: number): string {
  if (p >= 80) return styles.priorityHigh ?? "";
  if (p >= 50) return styles.priorityMedium ?? "";
  return styles.priorityLow ?? "";
}

interface HeaderDef {
  label: string;
  sortKey?: SortKey;
}
const HEADERS: HeaderDef[] = [
  { label: "Case ID" },
  { label: "State", sortKey: "queue_state" },
  { label: "Priority", sortKey: "priority" },
  { label: "Decision" },
  { label: "Confidence", sortKey: "confidence" },
  { label: "Assigned To" },
  { label: "SLA Due", sortKey: "sla_due_at" },
];

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }): ReactElement {
  return (
    <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden="true" style={{ flexShrink: 0, marginLeft: 4, verticalAlign: "middle" }}>
      <path d="M5 7L8 4L11 7" stroke={active && dir === "asc" ? "var(--accent)" : "var(--text-faint, #94a3b8)"} strokeWidth={active && dir === "asc" ? 2 : 1.5} strokeLinecap="round" strokeLinejoin="round" />
      <path d="M5 9L8 12L11 9" stroke={active && dir === "desc" ? "var(--accent)" : "var(--text-faint, #94a3b8)"} strokeWidth={active && dir === "desc" ? 2 : 1.5} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function buildApiQuery(args: {
  queue: QueueState;
  priority: Priority;
  assigned: string;
  personId: string;
  decision: string;
  engine: string;
  confMin: string;
  confMax: string;
  createdAfter: string;
  createdBefore: string;
  slaAfter: string;
  slaBefore: string;
  overdue: boolean;
  search: string;
  sortKey: SortKey | null;
  sortDir: SortDir;
}): URLSearchParams {
  const p = new URLSearchParams();
  if (args.queue === "unresolved") p.set("resolved", "false");
  else if (args.queue === "resolved") p.set("resolved", "true");
  else if (args.queue !== "all") p.set("queue_state", args.queue);
  const pr = PRIORITY_FILTERS.find((f) => f.value === args.priority);
  if (pr?.gte != null) p.set("priority_gte", String(pr.gte));
  if (pr?.lte != null) p.set("priority_lte", String(pr.lte));
  if (args.assigned.trim()) p.set("assigned_to", args.assigned.trim());
  if (args.personId.trim()) p.set("person_id", args.personId.trim());
  if (args.decision) p.set("decision", args.decision);
  if (args.engine) p.set("engine_type", args.engine);
  if (args.confMin.trim()) p.set("confidence_gte", args.confMin.trim());
  if (args.confMax.trim()) p.set("confidence_lte", args.confMax.trim());
  if (args.createdAfter) p.set("created_after", args.createdAfter);
  if (args.createdBefore) p.set("created_before", args.createdBefore);
  if (args.slaAfter) p.set("sla_due_after", args.slaAfter);
  if (args.slaBefore) p.set("sla_due_before", args.slaBefore);
  if (args.overdue) p.set("overdue_sla", "true");
  if (args.search.trim().length >= 3) p.set("q", args.search.trim());
  if (args.sortKey) {
    p.set("sort_by", args.sortKey);
    p.set("sort_order", args.sortDir);
  }
  return p;
}

type ReviewFilterKey = "state" | "priority" | "assigned" | "match" | "confidence" | "created" | "sla";

function FilterPill({
  label,
  isActive,
  activeLabel,
  count,
  open,
  onToggle,
  onClear,
  children,
}: {
  label: string;
  isActive: boolean;
  activeLabel: string;
  count?: number;
  open: boolean;
  onToggle: () => void;
  onClear: () => void;
  children: ReactNode;
}): ReactElement {
  return (
    <div className={styles.filterGroup}>
      <button
        type="button"
        className={`${styles.filterPill} ${isActive ? styles.filterPillActive : ""}`}
        onClick={onToggle}
        aria-expanded={open}
        aria-haspopup="true"
      >
        <span className={styles.fpLabel}>
          {isActive ? activeLabel : label}
          {isActive && count !== undefined && count > 1 ? <span className={styles.fpCount}>{count}</span> : null}
        </span>
        {isActive ? (
          <span
            role="button"
            className={styles.fpClear}
            tabIndex={0}
            aria-label={`Clear ${label} filter`}
            onClick={(event) => { event.stopPropagation(); onClear(); }}
            onKeyDown={(event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                event.stopPropagation();
                onClear();
              }
            }}
          >
            ×
          </span>
        ) : (
          <svg width="9" height="9" viewBox="0 0 12 12" fill="none" aria-hidden="true">
            <path d="M2.5 4.5L6 8L9.5 4.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </button>
      {open ? <div className={styles.filterPopover}>{children}</div> : null}
    </div>
  );
}

export default function ReviewPage(): ReactElement {
  const [queueFilter, setQueueFilter] = useState<QueueState>("open");
  const [priorityFilter, setPriorityFilter] = useState<Priority>("all");
  const [assignedFilter, setAssignedFilter] = useState("");
  const [personIdFilter, setPersonIdFilter] = useState("");
  const [decision, setDecision] = useState("");
  const [engine, setEngine] = useState("");
  const [confMin, setConfMin] = useState("");
  const [confMax, setConfMax] = useState("");
  const [createdAfter, setCreatedAfter] = useState("");
  const [createdBefore, setCreatedBefore] = useState("");
  const [slaAfter, setSlaAfter] = useState("");
  const [slaBefore, setSlaBefore] = useState("");
  const [overdue, setOverdue] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [openFilter, setOpenFilter] = useState<ReviewFilterKey | null>(null);
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [pageSize, setPageSize] = useState(25);

  // Cursor-based pagination
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [cursorStack, setCursorStack] = useState<(string | null)[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);

  // Data state
  const [rows, setRows] = useState<ReviewCaseSummary[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadId = useId();
  const setGlobalLoading = useSetLoading();

  // Debounce the free-text search before it reaches the server query.
  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedSearch(search), 300);
    return () => window.clearTimeout(handle);
  }, [search]);

  function toggleSort(key: SortKey): void {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir(DEFAULT_DIR[key]);
      return;
    }
    const next: SortDir = sortDir === "asc" ? "desc" : "asc";
    if (next === DEFAULT_DIR[key]) {
      setSortKey(null); // third click → reset to server default
    } else {
      setSortDir(next);
    }
  }

  // Base query (all server-side params except cursor/limit)
  const apiQuery = useMemo(
    () =>
      buildApiQuery({
        queue: queueFilter,
        priority: priorityFilter,
        assigned: assignedFilter,
        personId: personIdFilter,
        decision,
        engine,
        confMin,
        confMax,
        createdAfter,
        createdBefore,
        slaAfter,
        slaBefore,
        overdue,
        search: debouncedSearch,
        sortKey,
        sortDir,
      }).toString(),
    [queueFilter, priorityFilter, assignedFilter, personIdFilter, decision, engine, confMin, confMax, createdAfter, createdBefore, slaAfter, slaBefore, overdue, debouncedSearch, sortKey, sortDir],
  );

  // Reset to page 1 when filters/sort change
  const prevQuery = useRef(apiQuery);
  useEffect(() => {
    if (prevQuery.current !== apiQuery) {
      prevQuery.current = apiQuery;
      setCurrentCursor(null);
      setCursorStack([]);
    }
  }, [apiQuery]);

  // Fetch
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setLoading(true);
    setGlobalLoading(loadId, true);
    setError(null);

    const params = new URLSearchParams(apiQuery);
    params.set("limit", String(pageSize));
    if (currentCursor) params.set("cursor", currentCursor);

    void bffFetchEnvelope<ReviewCaseSummary[]>(`/bff/review-cases?${params.toString()}`, { signal })
      .then((res: ApiResponse<ReviewCaseSummary[]>) => {
        if (!signal.aborted) {
          setRows(res.data ?? []);
          setTotal(res.meta.total_count ?? null);
          setNextCursor(res.meta.next_cursor ?? null);
        }
      })
      .catch((err: unknown) => {
        if (!signal.aborted) {
          setError(err instanceof BffError ? err.message : "Failed to load review queue.");
        }
      })
      .finally(() => {
        if (!signal.aborted) { setLoading(false); setGlobalLoading(loadId, false); }
      });

    return () => { controller.abort(); setGlobalLoading(loadId, false); };
  }, [apiQuery, currentCursor, pageSize, loadId, setGlobalLoading]);

  const goNext = useCallback(() => {
    if (!nextCursor) return;
    setCursorStack((s) => [...s, currentCursor]);
    setCurrentCursor(nextCursor);
  }, [nextCursor, currentCursor]);

  const goPrev = useCallback(() => {
    const prev = cursorStack[cursorStack.length - 1] ?? null;
    setCursorStack((s) => s.slice(0, -1));
    setCurrentCursor(prev);
  }, [cursorStack]);

  const hasPrev = cursorStack.length > 0;
  const hasNext = nextCursor !== null;
  const pageStart = cursorStack.length * pageSize + 1;
  const pageEnd = cursorStack.length * pageSize + rows.length;

  const matchFilterCount = [personIdFilter, decision, engine].filter((value) => value !== "").length;
  const confidenceFilterCount = [confMin, confMax].filter((value) => value !== "").length;
  const createdFilterCount = [createdAfter, createdBefore].filter((value) => value !== "").length;
  const slaFilterCount = [slaAfter, slaBefore].filter((value) => value !== "").length + (overdue ? 1 : 0);
  const advancedActive =
    matchFilterCount > 0 || confidenceFilterCount > 0 || createdFilterCount > 0 || slaFilterCount > 0;
  const hasFilters =
    search !== "" || assignedFilter !== "" || queueFilter !== "open" ||
    priorityFilter !== "all" || sortKey !== null || advancedActive;

  function resetPage(): void {
    setCurrentCursor(null);
    setCursorStack([]);
  }

  function clearAll(): void {
    setSearch("");
    setAssignedFilter("");
    setPersonIdFilter("");
    setQueueFilter("open");
    setPriorityFilter("all");
    setDecision("");
    setEngine("");
    setConfMin("");
    setConfMax("");
    setCreatedAfter("");
    setCreatedBefore("");
    setSlaAfter("");
    setSlaBefore("");
    setOverdue(false);
    setSortKey(null);
    setOpenFilter(null);
  }

  return (
    <div className={styles.page}>

      <div className={styles.heading}>
        <h1 className={styles.title}>Review Queue</h1>
        {!loading && total !== null && <span className={styles.count}>{total}</span>}
      </div>

      <div className={styles.filterSection}>

        {/* Search row */}
        <div className={styles.searchRow}>
          <div className={styles.searchBox}>
            <svg className={styles.searchIcon} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input className={styles.searchInput} type="text" value={search}
              onChange={(e) => setSearch(e.target.value)} placeholder="Search by case ID, decision, person name…" />
          </div>
          {hasFilters && (
            <button type="button" className={styles.clearBtn} onClick={clearAll}>Clear all</button>
          )}
        </div>

        <div className={styles.filterBar}>
          <FilterPill
            label="State"
            isActive={queueFilter !== "all"}
            activeLabel={QUEUE_FILTERS.find((item) => item.value === queueFilter)?.label ?? "State"}
            open={openFilter === "state"}
            onToggle={() => setOpenFilter((current) => current === "state" ? null : "state")}
            onClear={() => setQueueFilter("open")}
          >
            <div className={styles.fpInner}>
              {QUEUE_FILTERS.map(({ value, label }) => (
                <button key={value} type="button" className={`${styles.fpOption} ${queueFilter === value ? styles.fpOptionActive : ""}`} onClick={() => { setQueueFilter(value); setOpenFilter(null); }}>
                  {label}
                </button>
              ))}
            </div>
          </FilterPill>

          <FilterPill
            label="Priority"
            isActive={priorityFilter !== "all"}
            activeLabel={PRIORITY_FILTERS.find((item) => item.value === priorityFilter)?.label ?? "Priority"}
            open={openFilter === "priority"}
            onToggle={() => setOpenFilter((current) => current === "priority" ? null : "priority")}
            onClear={() => setPriorityFilter("all")}
          >
            <div className={styles.fpInner}>
              {PRIORITY_FILTERS.map(({ value, label }) => (
                <button key={value} type="button" className={`${styles.fpOption} ${priorityFilter === value ? styles.fpOptionActive : ""}`} onClick={() => { setPriorityFilter(value); setOpenFilter(null); }}>
                  {label}
                </button>
              ))}
            </div>
          </FilterPill>

          <FilterPill
            label="Assigned"
            isActive={assignedFilter.trim() !== ""}
            activeLabel={assignedFilter.trim() || "Assigned"}
            open={openFilter === "assigned"}
            onToggle={() => setOpenFilter((current) => current === "assigned" ? null : "assigned")}
            onClear={() => setAssignedFilter("")}
          >
            <div className={styles.fpInner}>
              <label className={styles.advField}>
                <span className={styles.advLabel}>Assigned to</span>
                <input className={styles.advInputWide} type="text" value={assignedFilter} onChange={(e) => setAssignedFilter(e.target.value)} placeholder="Reviewer email or id…" />
              </label>
            </div>
          </FilterPill>

          <FilterPill
            label="Match"
            isActive={matchFilterCount > 0}
            activeLabel="Match"
            count={matchFilterCount}
            open={openFilter === "match"}
            onToggle={() => setOpenFilter((current) => current === "match" ? null : "match")}
            onClear={() => { setPersonIdFilter(""); setDecision(""); setEngine(""); }}
          >
            <div className={styles.fpInner}>
              <label className={styles.advField}>
                <span className={styles.advLabel}>Person ID</span>
                <input className={styles.advInputWide} type="text" value={personIdFilter} onChange={(e) => setPersonIdFilter(e.target.value)} placeholder="person id…" />
              </label>
              <label className={styles.advField}>
                <span className={styles.advLabel}>Decision</span>
                <select className={styles.advInputWide} value={decision} onChange={(e) => setDecision(e.target.value)}>
                  <option value="">Any</option>
                  {DECISION_OPTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
              </label>
              <label className={styles.advField}>
                <span className={styles.advLabel}>Engine</span>
                <select className={styles.advInputWide} value={engine} onChange={(e) => setEngine(e.target.value)}>
                  <option value="">Any</option>
                  {ENGINE_OPTIONS.map((e2) => <option key={e2} value={e2}>{e2}</option>)}
                </select>
              </label>
            </div>
          </FilterPill>

          <FilterPill
            label="Confidence"
            isActive={confidenceFilterCount > 0}
            activeLabel="Confidence"
            count={confidenceFilterCount}
            open={openFilter === "confidence"}
            onToggle={() => setOpenFilter((current) => current === "confidence" ? null : "confidence")}
            onClear={() => { setConfMin(""); setConfMax(""); }}
          >
            <div className={styles.fpInner}>
              <div className={styles.fpRow}>
                <label className={styles.advField}>
                  <span className={styles.advLabel}>Min</span>
                  <input className={styles.advInput} type="number" min={0} max={1} step={0.05} value={confMin} onChange={(e) => setConfMin(e.target.value)} placeholder="0.0" />
                </label>
                <label className={styles.advField}>
                  <span className={styles.advLabel}>Max</span>
                  <input className={styles.advInput} type="number" min={0} max={1} step={0.05} value={confMax} onChange={(e) => setConfMax(e.target.value)} placeholder="1.0" />
                </label>
              </div>
            </div>
          </FilterPill>

          <FilterPill
            label="Created"
            isActive={createdFilterCount > 0}
            activeLabel="Created"
            count={createdFilterCount}
            open={openFilter === "created"}
            onToggle={() => setOpenFilter((current) => current === "created" ? null : "created")}
            onClear={() => { setCreatedAfter(""); setCreatedBefore(""); }}
          >
            <div className={styles.fpInner}>
              <label className={styles.advField}>
                <span className={styles.advLabel}>Created after</span>
                <input className={styles.advInputWide} type="date" value={createdAfter} onChange={(e) => setCreatedAfter(e.target.value)} />
              </label>
              <label className={styles.advField}>
                <span className={styles.advLabel}>Created before</span>
                <input className={styles.advInputWide} type="date" value={createdBefore} onChange={(e) => setCreatedBefore(e.target.value)} />
              </label>
            </div>
          </FilterPill>

          <FilterPill
            label="SLA"
            isActive={slaFilterCount > 0}
            activeLabel="SLA"
            count={slaFilterCount}
            open={openFilter === "sla"}
            onToggle={() => setOpenFilter((current) => current === "sla" ? null : "sla")}
            onClear={() => { setSlaAfter(""); setSlaBefore(""); setOverdue(false); }}
          >
            <div className={styles.fpInner}>
              <button type="button" className={`${styles.fpOption} ${overdue ? styles.fpOptionActive : ""}`} onClick={() => setOverdue((value) => !value)}>
                Overdue SLA
              </button>
              <label className={styles.advField}>
                <span className={styles.advLabel}>SLA due after</span>
                <input className={styles.advInputWide} type="date" value={slaAfter} onChange={(e) => setSlaAfter(e.target.value)} />
              </label>
              <label className={styles.advField}>
                <span className={styles.advLabel}>SLA due before</span>
                <input className={styles.advInputWide} type="date" value={slaBefore} onChange={(e) => setSlaBefore(e.target.value)} />
              </label>
            </div>
          </FilterPill>
        </div>

        {/* Count + pagination bar */}
        <div className={styles.tableSection}>
          <div className={styles.countBar}>
            <span className={styles.resultCount}>
              {loading
                ? "Loading…"
                : rows.length === 0
                  ? "No results"
                  : `Showing ${pageStart}–${pageEnd}${total !== null ? ` of ${total}` : ""}`}
            </span>
            <div className={styles.tableTools}>
              <select
                className={styles.pageSizeSelect}
                value={pageSize}
                onChange={(e) => { setPageSize(Number(e.target.value)); resetPage(); }}
              >
                {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} / page</option>)}
              </select>
              {(hasPrev || hasNext) && (
                <div className={styles.paginationInline}>
                  <button className={styles.pageBtn} disabled={!hasPrev} onClick={goPrev}>← Prev</button>
                  <button className={styles.pageBtn} disabled={!hasNext} onClick={goNext}>Next →</button>
                </div>
              )}
            </div>
          </div>

          {error && <p className={styles.errorBanner}>{error}</p>}

          <div className={styles.tableScroll}>
            <table className={styles.table}>
            <thead>
              <tr>
                {HEADERS.map((h) => (
                  <th key={h.label} className={styles.th}
                    style={h.sortKey ? { cursor: "pointer" } : undefined}
                    onClick={h.sortKey ? () => toggleSort(h.sortKey as SortKey) : undefined}>
                    {h.label}
                    {h.sortKey && <SortIcon active={sortKey === h.sortKey} dir={sortKey === h.sortKey ? sortDir : "asc"} />}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {loading ? (
                Array.from({ length: 8 }, (_, i) => (
                  <tr key={i} className={styles.skeletonRow}>
                    {[80, 55, 45, 65, 40, 60, 50].map((w, j) => (
                      <td key={j} className={styles.td}>
                        <span className={styles.skeletonLine} style={{ width: `${w}%` }} />
                      </td>
                    ))}
                  </tr>
                ))
              ) : rows.length === 0 ? (
                <tr><td colSpan={HEADERS.length} className={styles.empty}>
                  {debouncedSearch ? `No results for "${debouncedSearch}"` : "No review cases found."}
                </td></tr>
              ) : (
                rows.map((c) => {
                  const confidencePct = Math.round(c.match_decision.confidence * 100);
                  const overdue = isOverdue(c.sla_due_at);
                  const subjectTitle = reviewSubjectTitle(c);
                  return (
                  <tr key={c.review_case_id} className={styles.tr}>
                    <td className={styles.td}>
                      <Link href={`/review/${c.review_case_id}`} className={styles.caseCellLink}>
                        <span className={styles.caseAvatar} style={{ background: subjectAvatarColor(subjectTitle) }}>
                          {subjectInitials(subjectTitle)}
                        </span>
                        <span className={styles.caseCopy}>
                          <span className={styles.caseTitle}>{subjectTitle}</span>
                          <span className={styles.caseMeta}>{reviewSubjectSubtitle(c)}</span>
                        </span>
                      </Link>
                    </td>
                    <td className={styles.td}>
                      <span className={`${styles.badge} ${queueStateClass(c.queue_state)}`}>
                        {c.queue_state}
                      </span>
                    </td>
                    <td className={styles.td}>
                      <span className={`${styles.priorityText} ${priorityClass(c.priority)}`}>
                        {priorityLabel(c.priority)}
                      </span>
                    </td>
                    <td className={styles.td}><span className={styles.decisionText}>{c.match_decision.decision}</span></td>
                    <td className={styles.td}>
                      <div className={styles.confidenceCell} aria-label={`${confidencePct}% confidence`}>
                        <div className={styles.confidenceTrack}>
                          <span className={styles.confidenceFill} style={{ width: `${confidencePct}%` }} />
                        </div>
                        <span className={styles.confidenceValue}>{confidencePct}%</span>
                      </div>
                    </td>
                    <td className={styles.td}>{c.assigned_to ?? <span className={styles.muted}>—</span>}</td>
                    <td className={styles.td}>
                      {c.sla_due_at
                        ? <span className={overdue ? styles.slaOverdue : styles.slaText}>
                            {relativeTime(c.sla_due_at)}
                          </span>
                        : <span className={styles.muted}>—</span>}
                    </td>
                  </tr>
                );})
              )}
            </tbody>
            </table>
          </div>

          <div className={`${styles.countBar} ${styles.countBarBottom}`}>
            <span className={styles.resultCount}>
              {loading
                ? "Loading…"
                : rows.length === 0
                  ? "No results"
                  : `Showing ${pageStart}–${pageEnd}${total !== null ? ` of ${total}` : ""}`}
            </span>
            <div className={styles.tableTools}>
              <select
                className={styles.pageSizeSelect}
                value={pageSize}
                onChange={(e) => setPageSize(Number(e.target.value))}
                aria-label="Rows per page"
              >
                {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} / page</option>)}
              </select>
              <div className={styles.paginationInline}>
                <button className={styles.pageBtn} disabled={!hasPrev} onClick={goPrev}>← Prev</button>
                <button className={styles.pageBtn} disabled={!hasNext} onClick={goNext}>Next →</button>
              </div>
            </div>
          </div>
        </div>

      </div>
    </div>
  );
}
