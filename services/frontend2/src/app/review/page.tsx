"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactElement } from "react";
import Link from "next/link";

import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ApiResponse } from "@/lib/api-types";
import type { ReviewCaseSummary } from "@/lib/api-types-ops";
import { completenessColor, relativeTime } from "@/lib/display";
import { useSetLoading } from "@/lib/LoadingContext";
import styles from "./review.module.css";

type QueueState = "open" | "assigned" | "deferred" | "all";
type Priority   = "all" | "high" | "medium" | "low";

const QUEUE_FILTERS: { value: QueueState; label: string }[] = [
  { value: "open",     label: "Open" },
  { value: "assigned", label: "Assigned" },
  { value: "deferred", label: "Deferred" },
  { value: "all",      label: "All" },
];

const PRIORITY_FILTERS: { value: Priority; label: string; lte: number | null }[] = [
  { value: "all",    label: "All priority", lte: null },
  { value: "high",   label: "High",         lte: 10 },
  { value: "medium", label: "Medium",       lte: 7 },
  { value: "low",    label: "Low",          lte: 4 },
];

const PAGE_SIZES = [25, 50, 100];

function queueStateColor(s: string): string {
  if (s === "open")     return "#3b82f6";
  if (s === "assigned") return "#f59e0b";
  if (s === "deferred") return "#94a3b8";
  return "#22c55e";
}
function priorityLabel(p: number): string {
  if (p >= 8) return "High";
  if (p >= 5) return "Medium";
  return "Low";
}
function priorityColor(p: number): string {
  if (p >= 8) return "var(--bad, #ef4444)";
  if (p >= 5) return "#f59e0b";
  return "var(--text-muted)";
}

const HEADERS = ["Case ID", "State", "Priority", "Decision", "Confidence", "Assigned To", "SLA Due"];

function buildApiQuery(queue: QueueState, priority: Priority, assigned: string): URLSearchParams {
  const p = new URLSearchParams();
  if (queue !== "all") p.set("queue_state", queue);
  const pr = PRIORITY_FILTERS.find((f) => f.value === priority);
  if (pr?.lte !== null && pr?.lte !== undefined) p.set("priority_lte", String(pr.lte));
  if (assigned.trim()) p.set("assigned_to", assigned.trim());
  return p;
}

export default function ReviewPage(): ReactElement {
  const [queueFilter,    setQueueFilter]    = useState<QueueState>("open");
  const [priorityFilter, setPriorityFilter] = useState<Priority>("all");
  const [assignedFilter, setAssignedFilter] = useState("");
  const [search,         setSearch]         = useState("");
  const [pageSize,       setPageSize]       = useState(25);

  // Cursor-based pagination
  const [currentCursor, setCurrentCursor] = useState<string | null>(null);
  const [cursorStack,   setCursorStack]   = useState<(string | null)[]>([]);
  const [nextCursor,    setNextCursor]    = useState<string | null>(null);

  // Data state
  const [rows,    setRows]    = useState<ReviewCaseSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error,   setError]   = useState<string | null>(null);

  const loadId = useId();
  const setGlobalLoading = useSetLoading();

  // Base query (server-side filters)
  const apiQuery = useMemo(
    () => buildApiQuery(queueFilter, priorityFilter, assignedFilter).toString(),
    [queueFilter, priorityFilter, assignedFilter],
  );

  // Reset to page 1 when filters change
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

  // Client-side search
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter(
      (c) =>
        c.review_case_id.toLowerCase().includes(q) ||
        c.match_decision.decision.toLowerCase().includes(q) ||
        (c.assigned_to?.toLowerCase().includes(q) ?? false),
    );
  }, [rows, search]);

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
  const pageEnd   = cursorStack.length * pageSize + filtered.length;

  const hasFilters = search || assignedFilter || queueFilter !== "open" || priorityFilter !== "all";
  function clearAll(): void {
    setSearch(""); setAssignedFilter(""); setQueueFilter("open"); setPriorityFilter("all");
  }

  return (
    <div className={styles.page}>

      <div className={styles.heading}>
        <h1 className={styles.title}>Review Queue</h1>
        {!loading && <span className={styles.count}>{filtered.length}</span>}
      </div>

      <div className={styles.filterSection}>

        {/* Search row */}
        <div className={styles.searchRow}>
          <div className={styles.searchBox}>
            <svg className={styles.searchIcon} width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
            <input className={styles.searchInput} type="text" value={search}
              onChange={(e) => setSearch(e.target.value)} placeholder="Search by case ID, decision…" />
          </div>
          <div className={styles.assignedBox}>
            <input className={styles.assignedInput} type="text" value={assignedFilter}
              onChange={(e) => setAssignedFilter(e.target.value)} placeholder="Assigned to…" />
          </div>
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
        </div>

        {/* Count + pagination bar */}
        <div className={styles.tableSection}>
          <div className={styles.countBar}>
            <span className={styles.resultCount}>
              {loading
                ? "Loading…"
                : `Showing ${filtered.length === 0 ? 0 : pageStart}–${pageEnd}`}
            </span>
            <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
              <select
                className={styles.pageSizeSelect}
                value={pageSize}
                onChange={(e) => { setPageSize(Number(e.target.value)); setCurrentCursor(null); setCursorStack([]); }}
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
              <tr>{HEADERS.map((h) => <th key={h} className={styles.th}>{h}</th>)}</tr>
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
              ) : filtered.length === 0 ? (
                <tr><td colSpan={HEADERS.length} className={styles.empty}>
                  {search ? `No results for "${search}"` : "No review cases found."}
                </td></tr>
              ) : (
                filtered.map((c) => (
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
