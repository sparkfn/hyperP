"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactElement } from "react";
import Link from "next/link";

import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ApiResponse } from "@/lib/api-types";
import type { ReviewCaseSummary } from "@/lib/api-types-ops";
import { completenessColor, relativeTime } from "@/lib/display";
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

function queueStateColor(s: string): string {
  if (s === "open") return "#3b82f6";
  if (s === "assigned") return "#f59e0b";
  if (s === "deferred") return "#94a3b8";
  return "#22c55e";
}
function priorityLabel(p: number): string {
  if (p >= 80) return "High";
  if (p >= 50) return "Medium";
  return "Low";
}
function priorityColor(p: number): string {
  if (p >= 80) return "var(--bad, #ef4444)";
  if (p >= 50) return "#f59e0b";
  return "var(--text-muted)";
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
  const [showMore, setShowMore] = useState(false);
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

  const advancedActive =
    personIdFilter !== "" || decision !== "" || engine !== "" || confMin !== "" || confMax !== "" ||
    createdAfter !== "" || createdBefore !== "" || slaAfter !== "" || slaBefore !== "" || overdue;
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
          <div className={styles.assignedBox}>
            <input className={styles.assignedInput} type="text" value={assignedFilter}
              onChange={(e) => setAssignedFilter(e.target.value)} placeholder="Assigned to…" />
          </div>
          <button type="button"
            className={`${styles.filterPill} ${showMore ? styles.filterPillActive : ""}`}
            onClick={() => setShowMore((v) => !v)} aria-expanded={showMore}>
            More filters{advancedActive ? " •" : ""}
          </button>
          {hasFilters && (
            <button type="button" className={styles.clearBtn} onClick={clearAll}>Clear all</button>
          )}
        </div>

        {/* Filter pills */}
        <div className={styles.filterBar}>
          {QUEUE_FILTERS.map(({ value, label }) => (
            <button key={value} type="button"
              className={`${styles.filterPill} ${queueFilter === value ? styles.filterPillActive : ""}`}
              onClick={() => setQueueFilter(value)}>
              {label}
            </button>
          ))}
          <div className={styles.filterSep} />
          {PRIORITY_FILTERS.map(({ value, label }) => (
            <button key={value} type="button"
              className={`${styles.filterPill} ${priorityFilter === value ? styles.filterPillActive : ""}`}
              onClick={() => setPriorityFilter(value)}>
              {label}
            </button>
          ))}
          <div className={styles.filterSep} />
          <button type="button"
            className={`${styles.filterPill} ${overdue ? styles.filterPillActive : ""}`}
            onClick={() => setOverdue((v) => !v)}>
            Overdue SLA
          </button>
        </div>

        {/* Advanced filters */}
        {showMore && (
          <div className={styles.advancedBar}>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Person ID</span>
              <input className={styles.advInput} type="text" value={personIdFilter}
                onChange={(e) => setPersonIdFilter(e.target.value)} placeholder="person id…" />
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Decision</span>
              <select className={styles.advSelect} value={decision} onChange={(e) => setDecision(e.target.value)}>
                <option value="">Any</option>
                {DECISION_OPTIONS.map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Engine</span>
              <select className={styles.advSelect} value={engine} onChange={(e) => setEngine(e.target.value)}>
                <option value="">Any</option>
                {ENGINE_OPTIONS.map((e2) => <option key={e2} value={e2}>{e2}</option>)}
              </select>
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Confidence ≥</span>
              <input className={styles.advInput} type="number" min={0} max={1} step={0.05}
                value={confMin} onChange={(e) => setConfMin(e.target.value)} placeholder="0.0" />
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Confidence ≤</span>
              <input className={styles.advInput} type="number" min={0} max={1} step={0.05}
                value={confMax} onChange={(e) => setConfMax(e.target.value)} placeholder="1.0" />
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Created after</span>
              <input className={styles.advInput} type="date" value={createdAfter} onChange={(e) => setCreatedAfter(e.target.value)} />
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>Created before</span>
              <input className={styles.advInput} type="date" value={createdBefore} onChange={(e) => setCreatedBefore(e.target.value)} />
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>SLA due after</span>
              <input className={styles.advInput} type="date" value={slaAfter} onChange={(e) => setSlaAfter(e.target.value)} />
            </label>
            <label className={styles.advField}>
              <span className={styles.advLabel}>SLA due before</span>
              <input className={styles.advInput} type="date" value={slaBefore} onChange={(e) => setSlaBefore(e.target.value)} />
            </label>
          </div>
        )}

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
            <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
              <select
                className={styles.pageSizeSelect}
                value={pageSize}
                onChange={(e) => { setPageSize(Number(e.target.value)); resetPage(); }}
              >
                {PAGE_SIZES.map((n) => <option key={n} value={n}>{n} / page</option>)}
              </select>
              {(hasPrev || hasNext) && (
                <>
                  <button className={styles.pageBtn} disabled={!hasPrev} onClick={goPrev}>← Prev</button>
                  <button className={styles.pageBtn} disabled={!hasNext} onClick={goNext}>Next →</button>
                </>
              )}
            </div>
          </div>

          {error && <p className={styles.error} style={{ padding: "0 14px 12px" }}>{error}</p>}

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
                rows.map((c) => (
                  <tr key={c.review_case_id} className={styles.tr}>
                    <td className={styles.td}>
                      <Link href={`/review/${c.review_case_id}`} className={styles.caseLink}>
                        {c.review_case_id.slice(0, 8)}…
                      </Link>
                    </td>
                    <td className={styles.td}>
                      <span className={styles.badge} style={{ color: queueStateColor(c.queue_state), background: `${queueStateColor(c.queue_state)}18` }}>
                        {c.queue_state}
                      </span>
                    </td>
                    <td className={styles.td}>
                      <span style={{ color: priorityColor(c.priority), fontWeight: 600, fontSize: 12 }}>
                        {priorityLabel(c.priority)}
                      </span>
                    </td>
                    <td className={styles.td}><span className={styles.mono}>{c.match_decision.decision}</span></td>
                    <td className={styles.td}>
                      <span style={{ color: completenessColor(c.match_decision.confidence), fontWeight: 600, fontSize: 12 }}>
                        {Math.round(c.match_decision.confidence * 100)}%
                      </span>
                    </td>
                    <td className={styles.td}>{c.assigned_to ?? <span className={styles.muted}>—</span>}</td>
                    <td className={styles.td}>
                      {c.sla_due_at
                        ? <span style={{ color: new Date(c.sla_due_at) < new Date() ? "var(--bad, #ef4444)" : "var(--text-secondary)", fontSize: 12 }}>
                            {relativeTime(c.sla_due_at)}
                          </span>
                        : <span className={styles.muted}>—</span>}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

      </div>
    </div>
  );
}
