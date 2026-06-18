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

export interface PaginatedSeed<T> {
  rows: T[];
  nextCursor: string | null;
  total: number | null;
}

export function seedFromEnvelope<T>(res: ApiResponse<T[]> | null): PaginatedSeed<T> | null {
  return res === null ? null : { rows: res.data, nextCursor: res.meta.next_cursor ?? null, total: res.meta.total_count ?? null };
}

export function usePaginatedFetch<T>(
  basePath: string,
  seedOrLimit?: PaginatedSeed<T> | null | number,
  limitOverride?: number,
): PaginatedResult<T> {
  const seed = typeof seedOrLimit === "number" ? null : seedOrLimit;
  const limit = typeof seedOrLimit === "number" ? seedOrLimit : (limitOverride ?? PAGE_SIZE);
  const [cursor, setCursor] = useState<string | null>(null);
  const [prevStack, setPrevStack] = useState<(string | null)[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(seed?.nextCursor ?? null);
  const [rows, setRows] = useState<T[] | null>(seed?.rows ?? null);
  const [total, setTotal] = useState<number | null>(seed?.total ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(seed == null);
  const setGlobalLoading = useSetLoading();
  const id = useId();
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
    const url = `${basePath}${sep}limit=${limit}${cursor !== null ? `&cursor=${encodeURIComponent(cursor)}` : ""}`;
    const controller = new AbortController();
    const run = async (): Promise<void> => {
      try {
        const res = await bffFetchEnvelope<T[]>(url, { signal: controller.signal });
        if (!cancelled) {
          setRows(res.data);
          setNextCursor(res.meta.next_cursor);
          setTotal(res.meta.total_count ?? null);
        }
      } catch (err: unknown) {
        if (!cancelled) {
          setRows([]);
          setNextCursor(null);
          setTotal(null);
          setError(err instanceof BffError ? err.message : "Failed to load data.");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
          setGlobalLoading(id, false);
        }
      }
    };
    void run();
    return () => {
      cancelled = true;
      controller.abort();
      setGlobalLoading(id, false);
    };
  }, [basePath, cursor, id, limit, setGlobalLoading]);

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

  const pageStart = prevStack.length * limit + 1;
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
