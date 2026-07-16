"use client";

import { useQuery } from "@tanstack/react-query";
import { useCallback, useState } from "react";

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

interface PageData<T> {
  rows: T[];
  nextCursor: string | null;
  total: number | null;
}

export function usePaginatedFetch<T>(basePath: string, limit: number = PAGE_SIZE): PaginatedResult<T> {
  const [cursor, setCursor] = useState<string | null>(null);
  const [prevStack, setPrevStack] = useState<(string | null)[]>([]);
  const query = useQuery({
    queryKey: ["bff-page", basePath, limit, cursor],
    queryFn: async ({ signal }): Promise<PageData<T>> => {
      const sep = basePath.includes("?") ? "&" : "?";
      const cursorQuery = cursor !== null ? `&cursor=${encodeURIComponent(cursor)}` : "";
      const url = `${basePath}${sep}limit=${limit}${cursorQuery}`;
      try {
        const envelope = await bffFetchEnvelope<T[]>(url, { signal });
        return {
          rows: envelope.data,
          nextCursor: envelope.meta.next_cursor,
          total: envelope.meta.total_count ?? null,
        };
      } catch (err: unknown) {
        if (err instanceof BffError && err.status === 404) {
          return { rows: [], nextCursor: null, total: 0 };
        }
        throw err;
      }
    },
  });

  const rows = query.data?.rows ?? null;
  const nextCursor = query.data?.nextCursor ?? null;
  const total = query.data?.total ?? null;

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
    error: query.error instanceof BffError
      ? query.error.message
      : query.error !== null ? "Failed to load." : null,
    loading: query.isPending,
    from: rows !== null && rows.length > 0 ? pageStart : 0,
    to: rows !== null ? pageStart + rows.length - 1 : 0,
    total,
    hasPrev: prevStack.length > 0,
    hasNext: nextCursor !== null,
    goNext,
    goPrev,
  };
}
