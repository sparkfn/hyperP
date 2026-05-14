"use client";

import { useCallback, useEffect, useState } from "react";
import { BffError, bffFetchEnvelope } from "@/lib/api-client";

export const PAGE_SIZE = 10;

export interface PaginatedResult<T> {
  rows: T[] | null;
  error: string | null;
  loading: boolean;
  from: number;
  to: number;
  total: number | null;
  hasPrev: boolean;
  hasNext: boolean;
  goNext: () => void;
  goPrev: () => void;
}

export function usePaginatedFetch<T>(basePath: string): PaginatedResult<T> {
  const [cursor, setCursor] = useState<string | null>(null);
  const [prevStack, setPrevStack] = useState<(string | null)[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [rows, setRows] = useState<T[] | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setRows(null);
    setError(null);
    const sep = basePath.includes("?") ? "&" : "?";
    const url = `${basePath}${sep}limit=${PAGE_SIZE}${cursor !== null ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    const run = async (): Promise<void> => {
      try {
        const envelope = await bffFetchEnvelope<T[]>(url);
        if (!cancelled) {
          setRows(envelope.data);
          setNextCursor(envelope.meta.next_cursor);
          setTotal(envelope.meta.total_count ?? null);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        setError(err instanceof BffError ? err.message : "Failed to load.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [basePath, cursor]);

  const goNext = useCallback((): void => {
    if (nextCursor === null) return;
    setPrevStack((prev) => [...prev, cursor]);
    setCursor(nextCursor);
  }, [cursor, nextCursor]);

  const goPrev = useCallback((): void => {
    const prevCursor = prevStack[prevStack.length - 1] ?? null;
    setPrevStack((stack) => stack.slice(0, -1));
    setCursor(prevCursor);
  }, [prevStack]);

  const pageStart = prevStack.length * PAGE_SIZE + 1;
  return {
    rows,
    error,
    loading,
    from: rows !== null && rows.length > 0 ? pageStart : 0,
    to: rows !== null ? pageStart + rows.length - 1 : 0,
    total,
    hasPrev: prevStack.length > 0,
    hasNext: nextCursor !== null,
    goNext,
    goPrev,
  };
}
