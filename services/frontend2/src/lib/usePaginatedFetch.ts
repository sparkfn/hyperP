"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";
import { BffError, bffFetchEnvelope } from "@/lib/api-client";
import type { ApiResponse } from "@/lib/api-types";
import { useSetLoading } from "@/lib/LoadingContext";

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

/** Page-0 data the caller already fetched, used to skip the initial network
 *  request. Must be a single `PAGE_SIZE` page so pagination stays consistent. */
export interface PaginatedSeed<T> {
  rows: T[];
  nextCursor: string | null;
  total: number | null;
}

/** Build a page-0 seed from an already-fetched envelope (or null on a skipped /
 *  failed fetch). The envelope must come from a `PAGE_SIZE` request. */
export function seedFromEnvelope<T>(res: ApiResponse<T[]> | null): PaginatedSeed<T> | null {
  return res ? { rows: res.data, nextCursor: res.meta.next_cursor, total: res.meta.total_count ?? null } : null;
}

export function usePaginatedFetch<T>(basePath: string, seed?: PaginatedSeed<T> | null): PaginatedResult<T> {
  const [cursor, setCursor] = useState<string | null>(null);
  const [prevStack, setPrevStack] = useState<(string | null)[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(seed?.nextCursor ?? null);
  const [rows, setRows] = useState<T[] | null>(seed?.rows ?? null);
  const [total, setTotal] = useState<number | null>(seed?.total ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(seed == null);
  const id = useId();
  const setGlobalLoading = useSetLoading();
  // Honour the seed only for the first render of the initial basePath + page 0.
  // Once consumed (or once the caller paginates / changes basePath) we fetch
  // normally. Callers that pass no seed are entirely unaffected.
  const seedForBasePath = useRef<string | null>(seed != null ? basePath : null);

  useEffect(() => {
    if (seedForBasePath.current === basePath && cursor === null) {
      seedForBasePath.current = null;
      setLoading(false);
      setGlobalLoading(id, false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setGlobalLoading(id, true);
    setRows(null);
    setError(null);
    const sep = basePath.includes("?") ? "&" : "?";
    const url = `${basePath}${sep}limit=${PAGE_SIZE}${cursor !== null ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    const controller = new AbortController();
    const run = async (): Promise<void> => {
      try {
        const envelope = await bffFetchEnvelope<T[]>(url, { signal: controller.signal });
        if (!cancelled) {
          setRows(envelope.data);
          setNextCursor(envelope.meta.next_cursor);
          setTotal(envelope.meta.total_count ?? null);
        }
      } catch (err: unknown) {
        if (cancelled) return;
        if (err instanceof BffError && err.status === 404) {
          setRows([]);
          setTotal(0);
        } else {
          setError(err instanceof BffError ? err.message : "Failed to load.");
        }
      } finally {
        if (!cancelled) { setLoading(false); setGlobalLoading(id, false); }
      }
    };
    void run();
    return () => {
      cancelled = true;
      controller.abort();
      setGlobalLoading(id, false);
    };
  }, [basePath, cursor, id, setGlobalLoading]);

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
